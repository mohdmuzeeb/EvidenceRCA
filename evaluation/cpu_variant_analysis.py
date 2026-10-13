"""Phase 3 CPU-fault investigation: standard (weak) vs strong cpu_pressure on dev cases only.

  uv run python -m evaluation.cpu_variant_analysis      # -> results/phase3/cpu_variant_analysis.json
"""

import json
import statistics
import subprocess
from pathlib import Path

from evaluation.run_dev import dev_cases
from evaluation.views import build
from evidencerca import rca_input
from evidencerca.core import detect_alert
from evidencerca.evidence import normalize
from evidencerca.methods.evidencerca_e0 import diagnose
from harness.validate import windows as gt_windows

OUT = Path("results/phase3/cpu_variant_analysis.json")


def cgroup_rates(monitor, svc, w):
    """Per-sample CPU cores and throttled-period ratio of one container inside window w (VM clock)."""
    vm = [m for m in monitor if m["kind"] == "vm"]
    cores, thr, total = [], [], []
    for a, b in zip(vm, vm[1:]):
        if not (w[0] <= b["vm_ns"] < w[1]):
            continue
        dt_us = (b["vm_ns"] - a["vm_ns"]) / 1000
        ca, cb = a["cgroup"].get(svc, {}), b["cgroup"].get(svc, {})
        if "usage_usec" in ca and "usage_usec" in cb and cb["usage_usec"] >= ca["usage_usec"]:
            cores.append((cb["usage_usec"] - ca["usage_usec"]) / dt_us)
            p = cb.get("nr_periods", 0) - ca.get("nr_periods", 0)
            if p > 0:
                thr.append((cb.get("nr_throttled", 0) - ca.get("nr_throttled", 0)) / p)
        tot = 0.0
        for s, cgs in b["cgroup"].items():
            prev = a["cgroup"].get(s, {})
            if "usage_usec" in cgs and "usage_usec" in prev:
                tot += max(cgs["usage_usec"] - prev["usage_usec"], 0) / dt_us
        total.append(tot)
    med = lambda xs: round(statistics.median(xs), 3) if xs else None
    return {"target_cores_median": med(cores), "throttled_ratio_median": med(thr),
            "all_containers_cores_median": med(total), "samples": len(cores)}


def main() -> int:
    rows = []
    for case in dev_cases():
        gt = case["gt"]
        if gt["fault_type"] != "cpu_pressure" or case["role"] != "primary":
            continue
        hdir = Path("data/ground_truth") / case["case_id"] / "harness"
        monitor = [json.loads(l) for l in (hdir / "monitor.jsonl").read_text().splitlines()]
        w = gt_windows(gt["timeline_vm_ns"])
        clean = normalize(rca_input.load_case(case["case_id"]))
        view, _ = build(clean, case["case_id"], "C0", 1, gt)
        alert = detect_alert(view)
        diag = diagnose(view, alert) if alert.triggered else None
        oi = gt["observable_impact"]
        rows.append({
            "case_id": case["case_id"], "target": gt["target"], "variant": gt["params"].get("variant", "standard"),
            "params": gt["params"], "fault_occurred": gt["validation"]["fault_occurred"],
            "recovered": gt["recovery"]["recovered"], "observable_impact": oi["gateway_slo_deviated"],
            "p95_ms": {"baseline": oi["baseline"]["p95_ms"], "injection": oi["injection_steady"]["p95_ms"]},
            "p50_ms": {"baseline": oi["baseline"]["p50_ms"], "injection": oi["injection_steady"]["p50_ms"]},
            "unexpected_rate": {"baseline": oi["baseline"]["unexpected_rate"],
                                "injection": oi["injection_steady"]["unexpected_rate"]},
            "agent_alert_triggered": alert.triggered,
            "e0_top1": diag["suspected_service"] if diag else None,
            "e0_fault_type": diag["suspected_fault_type"] if diag else None,
            "cgroup_baseline": cgroup_rates(monitor, gt["target"], w["baseline"]),
            "cgroup_injection": cgroup_rates(monitor, gt["target"], w["injection_steady"]),
            "quota_restored": gt["injection_actions"]["cleanup"].get("quota_restored"),
        })
    therm = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True).stdout.strip()
    report = {"schema": "evidencerca.phase3.cpu_variant/v1", "split": "dev (seed 11) only", "cases": rows,
              "host_thermal_state_after_runs": therm,
              "host_safety_argument": ("The strong variant lowers the target container's own CFS quota to 0.2 CPU; "
                                       "a container can then consume at most 0.2 cores, so host CPU load from the "
                                       "fault is lower than the standard variant's (bounded by 1.0). See "
                                       "all_containers_cores_median per window.")}
    OUT.write_text(json.dumps(report, indent=1) + "\n")
    for r in rows:
        print(r["variant"], r["target"], "impact", r["observable_impact"], "p95", r["p95_ms"], "alert",
              r["agent_alert_triggered"], "E0", r["e0_top1"], r["e0_fault_type"], "cores", r["cgroup_injection"])
    print(therm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
