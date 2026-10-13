"""The six fault types of the research plan (§4.2), each with inject() and cleanup().

Injectors only *act*. Whether the fault really took effect is decided later by
harness/validate.py from independent observations (see docs/fault_catalog.md).
Each action records a VM-clock timestamp taken by the mechanism itself (control
channel response, Docker state, `date` on the VM host, or Postgres clock).

Scope: every action targets one container of this Compose project (checked by
docker_ops.project_container), or one Postgres table of the testbed database.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from harness import docker_ops as d

NETEM_DELAY_MS = 2500           # > the 2.0 s caller timeout (DOWNSTREAM_TIMEOUT_S)
PROCESSING_DELAY_MS = 500       # < the caller timeout: slow but successful
POOL_SIZE = 5                   # DB_POOL_SIZE in compose.yaml
CPU_BURNERS = 2                 # busy processes inside a container capped at cpus: 1.0
LOCKED_TABLE = "items"          # read by inventory on every stock check and reservation

# Operation targeted by the processing-latency fault, per service.
LATENCY_OPERATION = {"gateway": ("POST", "/api/orders"), "order": ("POST", "/orders"),
                     "payment": ("POST", "/charge"), "inventory": ("POST", "/reserve")}

# The burner is identified for cleanup by this argv token; it exists only in /proc of the target container.
BURNER_TOKEN = "evr-cpu-burn"
BURNER_SRC = ("import sys,time\n"
              "end=time.time()+float(sys.argv[1])\n"
              "while time.time()<end: pass\n")


def parse_docker_ts(ts: str) -> int:
    """RFC3339 with nanoseconds (Docker state timestamps, VM clock) -> unix ns."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", ts)
    base = datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(3) == "Z" else m.group(3)))
    frac = (m.group(2) or "0").ljust(9, "0")[:9]
    return int(base.timestamp()) * 1_000_000_000 + int(frac)


@dataclass
class Context:
    target: str
    duration_s: float
    psql_injector: d.PsqlSession
    notes: dict = field(default_factory=dict)


class Fault:
    name: str
    eligible: tuple[str, ...]
    root_operation: str | None = None

    def params(self, target: str) -> dict:
        return {}

    def root_operation_for(self, target: str) -> str | None:
        return self.root_operation

    def inject(self, ctx: Context) -> dict:
        raise NotImplementedError

    def cleanup(self, ctx: Context) -> dict:
        raise NotImplementedError


class NoFault(Fault):
    name, eligible = "none", ("-",)

    def inject(self, ctx):
        return {"action": "none", "vm_ns": None}

    def cleanup(self, ctx):
        return {"action": "none", "vm_ns": None}


class ServiceCrash(Fault):
    name, eligible = "service_crash", ("order", "payment", "inventory")

    def params(self, target):
        return {"signal": "SIGKILL", "restart_on_cleanup": True}

    def inject(self, ctx):
        name = d.project_container(ctx.target)
        d.run("docker", "kill", "--signal", "KILL", name)
        st = d.inspect([ctx.target])[ctx.target]
        return {"action": "docker kill -s KILL", "vm_ns": parse_docker_ts(st["finished_at"]),
                "vm_ns_source": "docker State.FinishedAt"}

    def cleanup(self, ctx):
        name = d.project_container(ctx.target)
        d.run("docker", "start", name)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            st = d.inspect([ctx.target])[ctx.target]
            if st["health"] == "healthy":
                break
            time.sleep(1)
        return {"action": "docker start; wait healthy", "vm_ns": parse_docker_ts(st["started_at"]),
                "vm_ns_source": "docker State.StartedAt", "health": st["health"]}


class ProcessingLatency(Fault):
    name, eligible = "processing_latency", ("gateway", "order", "payment", "inventory")

    def params(self, target):
        method, path = LATENCY_OPERATION[target]
        return {"method": method, "path": path, "delay_ms": PROCESSING_DELAY_MS}

    def root_operation_for(self, target):
        method, path = LATENCY_OPERATION[target]
        return f"{method} {path}"

    def inject(self, ctx):
        p = self.params(ctx.target)
        r = d.control(ctx.target, "/delay", {"method": p["method"], "path": p["path"], "ms": p["delay_ms"]})
        return {"action": "control /delay", "vm_ns": r["applied_from_ns"], "vm_ns_source": "control channel clock",
                "state": r}

    def cleanup(self, ctx):
        r = d.control(ctx.target, "/delay/clear", {})
        return {"action": "control /delay/clear", "vm_ns": r["applied_from_ns"],
                "vm_ns_source": "control channel clock", "state": r}


class NetworkTimeout(Fault):
    name, eligible = "network_timeout", ("order", "payment", "inventory")

    def params(self, target):
        return {"qdisc": f"netem delay {NETEM_DELAY_MS}ms", "interface": "eth0", "direction": "egress",
                "caller_timeout_s": 2.0}

    def _nsenter(self, target: str, tc_args: str) -> int:
        d.project_container(target)
        pid = d.inspect([target])[target]["pid"]
        if not pid:
            raise d.HarnessError(f"{target} has no running process")
        out = d.vm(f"sudo nsenter -t {int(pid)} -n tc {tc_args} && date +%s%N")
        return int(out.strip().splitlines()[-1])

    def inject(self, ctx):
        ns = self._nsenter(ctx.target, f"qdisc add dev eth0 root netem delay {NETEM_DELAY_MS}ms")
        return {"action": "nsenter -n tc qdisc add ... netem", "vm_ns": ns, "vm_ns_source": "VM host date"}

    def cleanup(self, ctx):
        ns = self._nsenter(ctx.target, "qdisc del dev eth0 root")
        return {"action": "nsenter -n tc qdisc del", "vm_ns": ns, "vm_ns_source": "VM host date"}


class DbPoolExhaustion(Fault):
    name, eligible = "db_pool_exhaustion", ("order", "inventory")
    root_operation = "database connection pool"

    def params(self, target):
        return {"held_connections": POOL_SIZE, "pool_size": POOL_SIZE, "held_in_open_transaction": True}

    def inject(self, ctx):
        r = d.control(ctx.target, "/hold", {"n": POOL_SIZE, "deadline_s": 30}, timeout=45)
        return {"action": "control /hold", "vm_ns": r["time_ns"], "vm_ns_source": "control channel clock",
                "state": r}

    def cleanup(self, ctx):
        r = d.control(ctx.target, "/release", {})
        return {"action": "control /release", "vm_ns": r["time_ns"], "vm_ns_source": "control channel clock",
                "state": r}


class CpuPressure(Fault):
    name, eligible = "cpu_pressure", ("order", "payment", "inventory")

    def params(self, target):
        return {"burner_processes": CPU_BURNERS, "container_cpu_cap": 1.0}

    def inject(self, ctx):
        name = d.project_container(ctx.target)
        bound_s = ctx.duration_s + 15  # burners also stop by themselves if cleanup never runs
        launcher = ("import subprocess,sys,time\n"
                    f"src={BURNER_SRC!r}\n"
                    f"for _ in range({CPU_BURNERS}):\n"
                    f"    subprocess.Popen([sys.executable,'-c',src,'{bound_s}','{BURNER_TOKEN}'],"
                    "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                    "print(time.time_ns())\n")
        out = d.run("docker", "exec", name, "python", "-c", launcher).stdout
        return {"action": f"docker exec: {CPU_BURNERS} busy-loop processes", "vm_ns": int(out.strip()),
                "vm_ns_source": "container clock", "self_terminate_after_s": bound_s}

    def cleanup(self, ctx):
        # Killed from the VM host via the container's cgroup process list: no process is started inside it.
        d.project_container(ctx.target)
        cid = d.inspect([ctx.target])[ctx.target]["id"]
        out = d.vm(f"n=0; for p in $(cat /sys/fs/cgroup/docker/{cid}/cgroup.procs); do "
                   f"grep -qa {BURNER_TOKEN} /proc/$p/cmdline 2>/dev/null && sudo kill -9 $p && n=$((n+1)); done; "
                   "echo $n $(date +%s%N)")
        n, ns = out.split()
        return {"action": "VM host: kill busy-loop processes in the target cgroup", "killed": int(n),
                "vm_ns": int(ns), "vm_ns_source": "VM host date"}


class CascadingFailure(Fault):
    name, eligible = "cascading_failure", ("postgres",)
    root_operation = f"table lock on {LOCKED_TABLE}"

    def params(self, target):
        return {"lock": f"ACCESS EXCLUSIVE on {LOCKED_TABLE}", "held_in": "open transaction (pre-opened session)",
                "expected_propagation": ["inventory", "order", "gateway"]}

    def inject(self, ctx):
        out = ctx.psql_injector.query(
            f"BEGIN; LOCK TABLE {LOCKED_TABLE} IN ACCESS EXCLUSIVE MODE; "
            "SELECT (extract(epoch from clock_timestamp())*1e9)::bigint;")
        return {"action": "psql: BEGIN; LOCK TABLE ... ACCESS EXCLUSIVE", "vm_ns": int(out[-1]),
                "vm_ns_source": "postgres clock_timestamp()"}

    def cleanup(self, ctx):
        out = ctx.psql_injector.query("COMMIT; SELECT (extract(epoch from clock_timestamp())*1e9)::bigint;")
        return {"action": "psql: COMMIT", "vm_ns": int(out[-1]), "vm_ns_source": "postgres clock_timestamp()"}


FAULTS: dict[str, Fault] = {f.name: f for f in (NoFault(), ServiceCrash(), ProcessingLatency(), NetworkTimeout(),
                                                 DbPoolExhaustion(), CpuPressure(), CascadingFailure())}
