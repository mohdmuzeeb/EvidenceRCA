"""Ground-truth monitor: the same observations, at the same cadence, in every case.

Symmetry is deliberate. Whatever small footprint the monitor has (reads on the VM
host, one pre-opened Postgres session, Docker inspect calls from the host), it is
identical in control and fault cases and across time, so it cannot mark when or
where a fault happened. Nothing is ever executed inside a service container. None of these observations touch the
injected mechanism's own state path, which is what makes them independent.

Streams written to <harness_dir>/monitor.jsonl (one JSON object per line, `kind`):
  docker   - container state from `docker inspect` (host)            every interval
  vm       - per-container cgroup cpu.stat / memory, netns qdisc    every interval
             (read on the VM host, VM clock), VM MemAvailable
  pg       - pg_stat_activity by (application, state); relation      every interval
             locks by (mode, granted, application, table)
  stats    - `docker stats` for all project containers              every 3rd interval
  host     - macOS memory (vm_stat, memorystatus level)             every 6th interval
  (vm also carries, per app service: control-channel state read in the container's network namespace and the
   number of busy-loop processes in its cgroup. These read back injector state, so they are recorded but not
   counted as independent evidence; they are used to verify that cleanup cleared everything.)
"""

import json
import re
import threading
import time

from harness import docker_ops as d
from harness.faults import BURNER_TOKEN

class Monitor:
    def __init__(self, out_path, psql: d.PsqlSession, interval_s: float = 5.0):
        self.out = open(out_path, "a")
        self.psql = psql
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="gt-monitor")
        self.errors = 0
        self.side_threads: list[threading.Thread] = []

    def write(self, rec: dict) -> None:
        with self.lock:
            if self.out.closed:  # a side sampler finishing after stop(): nothing to record
                return
            self.out.write(json.dumps(rec) + "\n")
            self.out.flush()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=60)
        for t in self.side_threads:
            t.join(timeout=60)
        with self.lock:
            self.out.close()

    # ------------------------------------------------------------------ samplers
    def sample_vm(self, states: dict) -> None:
        parts = ["echo T0 $(date +%s%N)", "echo MEMAVAIL $(awk '/MemAvailable/{print $2}' /proc/meminfo)"]
        for svc in d.SERVICES:
            st = states.get(svc) or {}
            cid = st.get("id")
            if cid and re.fullmatch(r"[0-9a-f]{64}", cid):
                parts.append(f"echo CG {svc} $(cat /sys/fs/cgroup/docker/{cid}/cpu.stat 2>/dev/null | tr '\\n' ' ')"
                             f" memory_current $(cat /sys/fs/cgroup/docker/{cid}/memory.current 2>/dev/null)")
            pid = st.get("pid")
            if svc in d.APP_SERVICES and st.get("running") and pid:
                ns = f"sudo nsenter -t {int(pid)} -n"
                parts.append(f"echo QD {svc} $({ns} tc qdisc show dev eth0 2>&1 | tr '\\n' ';')")
                # control-channel state, read from the VM host in the container's network namespace only
                parts.append(f"echo CTL {svc} $({ns} curl -s -m 3 http://127.0.0.1:9000/state)")
                # busy-loop processes in the container's cgroup, counted from the VM host
                parts.append(f"n=0; for p in $(cat /sys/fs/cgroup/docker/{cid}/cgroup.procs 2>/dev/null); do "
                             f"grep -qa {BURNER_TOKEN} /proc/$p/cmdline 2>/dev/null && n=$((n+1)); done; "
                             f"echo BURN {svc} $n")
        parts.append("echo T1 $(date +%s%N)")
        h0 = time.time_ns()
        out = d.vm("; ".join(parts))
        h1 = time.time_ns()
        rec = {"kind": "vm", "host_ns": (h0 + h1) // 2, "cgroup": {}, "qdisc": {}}
        for line in out.splitlines():
            tag, _, rest = line.partition(" ")
            if tag in ("T0", "T1"):
                rec[tag.lower() + "_vm_ns"] = int(rest)
            elif tag == "MEMAVAIL":
                rec["vm_mem_available_kib"] = int(rest or 0)
            elif tag == "CG":
                svc, _, kv = rest.partition(" ")
                toks = kv.split()
                rec["cgroup"][svc] = {toks[i]: int(toks[i + 1]) for i in range(0, len(toks) - 1, 2)
                                      if toks[i + 1].isdigit()}
            elif tag == "QD":
                svc, _, q = rest.partition(" ")
                rec["qdisc"][svc] = q.strip()
            elif tag == "CTL":
                svc, _, js = rest.partition(" ")
                try:
                    rec.setdefault("control", {})[svc] = json.loads(js)
                except ValueError:
                    rec.setdefault("control", {})[svc] = {"error": js[:100]}
            elif tag == "BURN":
                svc, _, n = rest.partition(" ")
                rec.setdefault("busy_processes", {})[svc] = int(n or 0)
        rec["vm_ns"] = (rec.get("t0_vm_ns", 0) + rec.get("t1_vm_ns", 0)) // 2
        rec["clock_offset_ns"] = rec["vm_ns"] - rec["host_ns"]
        rec["clock_offset_uncertainty_ns"] = (h1 - h0) // 2
        self.write(rec)

    def sample_pg(self) -> None:
        rows = self.psql.query(
            "SELECT 'clk', (extract(epoch from clock_timestamp())*1e9)::bigint;"
            "SELECT 'act', coalesce(application_name,''), coalesce(state,''), count(*) FROM pg_stat_activity "
            "WHERE datname = 'shop' AND pid <> pg_backend_pid() GROUP BY 2, 3;"
            "SELECT 'lck', l.mode, l.granted, coalesce(a.application_name,''), c.relname, count(*) "
            "FROM pg_locks l JOIN pg_class c ON c.oid = l.relation LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'relation' AND l.pid <> pg_backend_pid() "
            "AND c.relname IN ('items', 'orders', 'reservations') GROUP BY 2, 3, 4, 5;")
        rec = {"kind": "pg", "host_ns": time.time_ns(), "activity": [], "locks": []}
        for r in rows:
            f = r.split("|")
            if f[0] == "clk":
                rec["vm_ns"] = int(f[1])
            elif f[0] == "act":
                rec["activity"].append({"app": f[1], "state": f[2], "n": int(f[3])})
            elif f[0] == "lck":
                rec["locks"].append({"mode": f[1], "granted": f[2] == "t", "app": f[3], "table": f[4],
                                     "n": int(f[5])})
        self.write(rec)

    def sample_stats(self) -> None:
        out = d.run("docker", "stats", "--no-stream", "--format", "{{json .}}", check=False, timeout=60).stdout
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.write({"kind": "stats", "host_ns": time.time_ns(),
                    "containers": {r["Name"]: {k: r[k] for k in ("CPUPerc", "MemUsage", "MemPerc", "NetIO",
                                                                  "BlockIO", "PIDs")}
                                   for r in rows if r["Name"].startswith(d.PROJECT + "-")}})

    def sample_host(self) -> None:
        vm = d.run("vm_stat", check=False).stdout
        page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size" in vm else 16384
        pages = {k.strip(): int(v) for k, v in re.findall(r"^([^:]+):\s+(\d+)\.", vm, re.M)}
        level = d.run("sysctl", "-n", "kern.memorystatus_level", check=False).stdout.strip()
        gib = 1024 ** 3
        self.write({"kind": "host", "host_ns": time.time_ns(),
                    "memorystatus_free_pct": int(level) if level.isdigit() else None,
                    "mem_gib": {"free": round(pages.get("Pages free", 0) * page / gib, 3),
                                "compressed": round(pages.get("Pages occupied by compressor", 0) * page / gib, 3),
                                "swapouts": pages.get("Swapouts", 0)}})

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        tick = 0
        next_t = time.monotonic()
        while not self.stop_event.is_set():
            try:
                states = d.inspect(d.SERVICES)
                self.write({"kind": "docker", "host_ns": time.time_ns(), "state": states})
                self.sample_vm(states)
                self.sample_pg()
                if tick % 3 == 0:
                    t = threading.Thread(target=self._safe, args=(self.sample_stats,), daemon=True)
                    self.side_threads = [x for x in self.side_threads if x.is_alive()] + [t]
                    t.start()
                if tick % 6 == 0:
                    self.sample_host()
            except Exception as exc:  # keep sampling; errors are counted and recorded
                self.errors += 1
                self.write({"kind": "error", "host_ns": time.time_ns(), "error": repr(exc)[:300]})
            tick += 1
            next_t += self.interval_s
            self.stop_event.wait(max(0.0, next_t - time.monotonic()))

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.errors += 1
            self.write({"kind": "error", "host_ns": time.time_ns(), "error": repr(exc)[:300]})
