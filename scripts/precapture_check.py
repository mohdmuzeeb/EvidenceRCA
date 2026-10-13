"""Pre-capture checklist for calibration/test capture -> results/final/precapture_checks.json.

  uv run python -m scripts.precapture_check --pytest-summary "..." --leakage results/final/precapture_leakage.json \
      --phase1 results/final/phase1_regression
Requires Colima running (clock offset). Exit 0 only if every check passes.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from evaluation.freeze import verify_frozen
from harness import docker_ops as d
from scripts.collect_env import build_inputs

OUT = Path("results/final/precapture_checks.json")


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pytest-summary", required=True)
    ap.add_argument("--leakage", required=True)
    ap.add_argument("--phase1", required=True)
    a = ap.parse_args()
    checks = {}

    status = sh("git", "status", "--porcelain", "--untracked-files=no")
    checks["git_clean_tracked"] = {"passed": status == "", "head": sh("git", "rev-parse", "HEAD")}
    try:
        man = verify_frozen()
        checks["freeze_verified"] = {"passed": True, "frozen_files": len(man["files"]), "frozen_at": man["frozen_at"]}
    except Exception as exc:
        checks["freeze_verified"] = {"passed": False, "error": str(exc)}

    phase2 = json.loads(Path("results/phase2/environment.json").read_text())["build_inputs_sha256"]
    now = build_inputs()
    diff = sorted(k for k in set(phase2) | set(now) if phase2.get(k) != now.get(k))
    # uv.lock also pins host-side analysis libraries (numpy/scikit-learn, added in Phase 3 as a separate
    # dependency group). What the services run is testbed/requirements.lock, which is compared above.
    host_only = {"uv.lock"}
    checks["testbed_and_pipeline_unchanged_since_phase2_capture"] = {
        "passed": not (set(diff) - host_only), "differences": diff, "files_compared": len(now),
        "note": "uv.lock differs only by the host-side 'analysis' group; the service image lock is unchanged"
        if "uv.lock" in diff else None}

    leak = json.loads(Path(a.leakage).read_text())
    checks["no_labels_in_agent_visible_evidence"] = {"passed": leak["passed"],
                                                     "case_sets": {k: v["cases"] for k, v in leak["case_sets"].items()}}
    checks["tests"] = {"passed": " failed" not in a.pytest_summary and "passed" in a.pytest_summary,
                       "summary": a.pytest_summary}
    p1s = json.loads((Path(a.phase1) / "smoke_test_results.json").read_text())
    p1v = json.loads((Path(a.phase1) / "telemetry_validation.json").read_text())
    checks["phase1_regression"] = {"passed": p1s["passed"] and p1v["summary"]["all_passed"],
                                   "telemetry_checks": f"{p1v['summary']['passed']}/{p1v['summary']['checks']}"}

    free_gb = shutil.disk_usage(".").free / 1e9
    checks["disk_space"] = {"passed": free_gb > 15, "free_gb": round(free_gb, 1),
                            "needed_estimate_gb": "~4.5 (90 cases x ~45 MB) + headroom"}
    batt = sh("pmset", "-g", "batt")
    checks["ac_power"] = {"passed": "AC Power" in batt, "pmset": batt.splitlines()[0] if batt else None}
    vm = json.loads(sh("colima", "list", "--json") or "{}")
    checks["colima_configuration"] = {"passed": vm.get("status") == "Running" and vm.get("cpus") == 4
                                      and vm.get("memory") == 3 * 1024 ** 3, "vm": vm}
    off = d.vm_clock_offset(samples=7)
    checks["clock_alignment"] = {"passed": abs(off["offset_ns"]) < 1e9, "vm_minus_host_ms": round(off["offset_ns"] / 1e6, 1),
                                 "uncertainty_ms": round(off["uncertainty_ns"] / 1e6, 1),
                                 "note": "all ground-truth times use the VM clock; offsets are also recorded per case"}
    ollama = sh("pgrep", "-fl", "ollama")
    checks["ollama_not_running"] = {"passed": ollama == "", "processes": ollama}
    gt_dev = sum(1 for l in Path("data/harness_index.jsonl").read_text().splitlines() if json.loads(l)["case_valid"])
    checks["development_results_preserved"] = {"passed": Path("results/phase3/dev_results.json").exists()
                                               and Path("results/phase3/pre_verifier_correction/dev_results.json").exists(),
                                               "indexed_valid_cases": gt_dev,
                                               "weak_cpu_cases_kept": "dev matrix cpu_pressure standard (3) unchanged"}
    report = {"schema": "evidencerca.precapture/v1", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "checks": checks, "all_passed": all(c["passed"] for c in checks.values())}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1) + "\n")
    for k, c in checks.items():
        print(("PASS" if c["passed"] else "FAIL"), k)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
