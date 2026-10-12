"""Reproducibility manifest for a testbed run.

Records OS/hardware, toolchain and dependency versions, image digests, git
state, the resolved compose configuration (with secrets redacted) and
host/VM resource usage. Deliberately omits hostnames, user names and absolute
paths.
"""

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEYS = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY", re.I)


def sh(*cmd: str) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=ROOT).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def redact(value):
    if isinstance(value, dict):
        return {k: "<redacted>" if SECRET_KEYS.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1<redacted>@", value)  # passwords in URLs
        return value.replace(str(ROOT), "<repo>").replace(str(Path.home()), "~")
    return value


def host() -> dict:
    gib = 1024 ** 3
    disk = shutil.disk_usage(ROOT)
    vm = sh("vm_stat") or ""
    page = int(re.search(r"page size of (\d+)", vm).group(1)) if "page size" in vm else 16384
    pages = {k.strip(): int(v.strip(" .")) for k, v in re.findall(r"^([^:]+):\s+(\d+)\.", vm, re.M)}
    return {
        "os": {"system": platform.system(), "release": platform.release(),
               "product_version": sh("sw_vers", "-productVersion"), "build": sh("sw_vers", "-buildVersion")},
        "hardware": {"machine": platform.machine(), "cpu": sh("sysctl", "-n", "machdep.cpu.brand_string"),
                     "logical_cpus": int(sh("sysctl", "-n", "hw.ncpu") or 0),
                     "memory_gib": round(int(sh("sysctl", "-n", "hw.memsize") or 0) / gib, 2)},
        "disk_gib": {"total": round(disk.total / gib, 1), "used": round(disk.used / gib, 1),
                     "free": round(disk.free / gib, 1)},
        "memory_gib": {k: round(pages.get(name, 0) * page / gib, 2) for k, name in
                       [("free", "Pages free"), ("active", "Pages active"), ("inactive", "Pages inactive"),
                        ("wired", "Pages wired down"), ("compressed", "Pages occupied by compressor")]},
        "load_average": list(map(float, (sh("sysctl", "-n", "vm.loadavg") or "{ 0 0 0 }").strip("{} ").split())),
    }


def toolchain() -> dict:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {
        "python_host": platform.python_version(),
        "uv": sh("uv", "--version"),
        "git": sh("git", "--version"),
        "docker_client": sh("docker", "version", "--format", "{{.Client.Version}}"),
        "docker_server": sh("docker", "version", "--format", "{{.Server.Version}}"),
        "docker_compose": sh("docker", "compose", "version", "--short"),
        "colima": (sh("colima", "version") or "").splitlines()[:1],
        "colima_vm": json.loads(sh("colima", "list", "--json") or "{}"),
        "ollama": sh("ollama", "--version"),
        "ollama_models": sh("ollama", "list"),
        "locked_packages": {p["name"]: p["version"] for p in lock["package"] if "version" in p},
    }


def images(compose_cfg: dict) -> list[dict]:
    out = []
    for name in sorted({svc["image"] for svc in compose_cfg.get("services", {}).values()}):
        info = json.loads(sh("docker", "image", "inspect", name) or "[]")
        if info:
            i = info[0]
            out.append({"reference": name, "image_id": i["Id"], "repo_digests": i.get("RepoDigests", []),
                        "architecture": i.get("Architecture"), "size_mb": round(i["Size"] / 1e6, 1),
                        "created": i.get("Created")})
        else:
            out.append({"reference": name, "image_id": None})
    return out


def build_inputs() -> dict:
    """The locally built service image has no registry digest; hash what it is built from instead."""
    files = ["testbed/Dockerfile", "testbed/requirements.lock", "uv.lock", "compose.yaml",
             "testbed/otel-collector.yaml", "testbed/postgres/init.sql", *sorted(
                 str(p.relative_to(ROOT)) for p in (ROOT / "testbed" / "app").glob("*.py"))]
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files}


def vm_resources() -> dict:
    return {"free_m": sh("colima", "ssh", "--", "free", "-m"),
            "df_root": sh("colima", "ssh", "--", "df", "-h", "/"),
            "docker_system_df": sh("docker", "system", "df")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, help="label for this snapshot, e.g. pre or post")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    compose_cfg = json.loads(sh("docker", "compose", "config", "--format", "json") or "{}")
    limits = {name: {"mem_limit": svc.get("mem_limit"), "cpus": svc.get("cpus"), "image": svc.get("image"),
                     "command": svc.get("command")}
              for name, svc in compose_cfg.get("services", {}).items()}
    manifest = {
        "schema": "evidencerca.environment/v1",
        "stage": args.stage,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host(),
        "toolchain": toolchain(),
        "git": {"commit": sh("git", "rev-parse", "HEAD"), "dirty": bool(sh("git", "status", "--porcelain"))},
        "images": images(compose_cfg),
        "build_inputs_sha256": build_inputs(),
        "service_limits": limits,
        "compose_config_redacted": redact(compose_cfg),
        "vm": vm_resources(),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(redact(manifest), indent=2) + "\n")
    print(f"environment manifest ({args.stage}) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
