"""Host-side operations on the Compose project and the Colima VM.

Every container-level action goes through `project_container()`, which refuses
any container that does not carry this project's Compose label, so the harness
cannot touch containers of other projects. VM-level commands (`colima ssh`) only
read cgroup files or act inside one project container's network namespace.
"""

import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "evidencerca"
SERVICES = ["gateway", "order", "payment", "inventory", "postgres"]
APP_SERVICES = ["gateway", "order", "payment", "inventory"]


class HarnessError(RuntimeError):
    pass


def run(*cmd: str, env: dict | None = None, timeout: float = 120, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, input=input_text,
                         env={**os.environ, **(env or {})})
    if check and res.returncode != 0:
        raise HarnessError(f"{' '.join(cmd[:4])}... failed ({res.returncode}): {res.stderr.strip()[:400]}")
    return res


def compose(*args: str, env: dict | None = None, timeout: float = 300, check: bool = True):
    return run("docker", "compose", *args, env=env, timeout=timeout, check=check)


def project_container(service: str) -> str:
    """Container name of a project service, after checking its Compose labels."""
    if service not in SERVICES:
        raise HarnessError(f"refusing to act on non-project service {service!r}")
    name = f"{PROJECT}-{service}-1"
    labels = json.loads(run("docker", "inspect", "-f", "{{json .Config.Labels}}", name).stdout)
    if labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != service:
        raise HarnessError(f"{name} is not service {service!r} of project {PROJECT!r}")
    return name


def inspect(services: list[str]) -> dict:
    names = [f"{PROJECT}-{s}-1" for s in services]
    res = run("docker", "inspect", *names, check=False)
    out = {}
    for c in json.loads(res.stdout or "[]"):
        svc = c["Config"]["Labels"].get("com.docker.compose.service")
        st = c["State"]
        out[svc] = {"id": c["Id"], "running": st["Running"], "status": st["Status"], "pid": st["Pid"],
                    "exit_code": st["ExitCode"], "oom_killed": st["OOMKilled"], "started_at": st["StartedAt"],
                    "finished_at": st["FinishedAt"], "restart_count": c.get("RestartCount", 0),
                    "health": (st.get("Health") or {}).get("Status")}
    return out


def vm(script: str, timeout: float = 30) -> str:
    """Run a shell script on the Colima VM host (not in a container)."""
    return run("colima", "ssh", "--", "sh", "-c", script, timeout=timeout).stdout


def vm_clock_offset(samples: int = 5) -> dict:
    """offset = VM clock - host clock (ns), from the tightest of several bracketed reads."""
    best = None
    for _ in range(samples):
        h0 = time.time_ns()
        v = int(vm("date +%s%N").strip())
        h1 = time.time_ns()
        cand = {"offset_ns": v - (h0 + h1) // 2, "uncertainty_ns": (h1 - h0) // 2, "host_ns": (h0 + h1) // 2}
        if best is None or cand["uncertainty_ns"] < best["uncertainty_ns"]:
            best = cand
    return best


def control(service: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    """Call a service's loopback control channel (127.0.0.1:9000 inside the container).

    The request is made from the VM host inside the container's *network* namespace only
    (`nsenter -n`), so no process is started in the container: nothing appears in its cgroup
    CPU/memory accounting, and therefore nothing in the agent-visible container metrics.
    """
    project_container(service)
    pid = inspect([service])[service]["pid"]
    if not pid:
        raise HarnessError(f"{service} is not running")
    method = "GET" if body is None else "POST"
    payload = base64.b64encode(json.dumps(body or {}).encode()).decode()
    out = vm(f"echo {payload} | base64 -d | sudo nsenter -t {int(pid)} -n curl -sS -f -m {int(timeout)} "
             f"-X {method} -H 'content-type: application/json' --data-binary @- "
             f"http://127.0.0.1:9000{path}", timeout=timeout + 15)
    return json.loads(out)


class PsqlSession:
    """One long-lived psql session inside the postgres container.

    Opened once per case before warm-up (in every case, faulted or not), so the
    single `connection authorized` log line it causes is identical across cases
    and carries no timing information about any injection. The session disables
    its own slow-statement logging.
    """

    _MARK = "__evr_end__"

    def __init__(self):
        name = project_container("postgres")
        self.proc = subprocess.Popen(
            ["docker", "exec", "-i", name, "psql", "-U", "shop", "-d", "shop", "-At", "-F", "|", "-q",
             "--no-psqlrc", "-v", "ON_ERROR_STOP=0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.lock = threading.Lock()
        self.query("SET log_min_duration_statement = -1; SET log_statement = 'none';")

    def query(self, sql: str, timeout: float = 30) -> list[str]:
        with self.lock:
            self.proc.stdin.write(sql.rstrip() + f"\n\\echo {self._MARK}\n")
            self.proc.stdin.flush()
            lines, deadline = [], time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    raise HarnessError("psql session ended")
                line = line.rstrip("\n")
                if line == self._MARK:
                    return lines
                lines.append(line)
            raise HarnessError(f"psql query timed out: {sql[:80]}")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
