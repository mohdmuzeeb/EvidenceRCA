"""Build results/phase2/pilot_results.json and ground_truth_validation.json from the recorded cases.

Reads data/harness_index.jsonl (case_id -> group/label) and each case's ground_truth.json.
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evidencerca.paths import DATA_ROOT, GROUND_TRUTH_ROOT
from harness.schedule import MECHANISM_CHECK_TIMELINE, PROTOCOL_TIMELINE, SPLITS

RESULTS = Path("results/phase2")
GT_ROOTS = {"pilot": GROUND_TRUTH_ROOT, "mechanism": DATA_ROOT / "mechanism" / "ground_truth",
            "load": DATA_ROOT / "load" / "ground_truth"}


def mem_mib(s: str) -> float:
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", s or "")
    if not m:
        return 0.0
    return float(m.group(1)) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "kB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]


def resources(gt_dir: Path) -> dict:
    peak_mem, peak_cpu = defaultdict(float), defaultdict(float)
    host_min_level, vm_min_avail, offsets = None, None, []
    for line in (gt_dir / "harness" / "monitor.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] == "stats":
            for name, c in r["containers"].items():
                svc = name.removeprefix("evidencerca-").removesuffix("-1")
                svc = "workload" if svc.startswith("workload-run") else svc
                peak_mem[svc] = max(peak_mem[svc], mem_mib(c["MemUsage"].split("/")[0].strip()))
                peak_cpu[svc] = max(peak_cpu[svc], float(c["CPUPerc"].rstrip("%") or 0))
        elif r["kind"] == "host" and r.get("memorystatus_free_pct") is not None:
            host_min_level = min(host_min_level or 100, r["memorystatus_free_pct"])
        elif r["kind"] == "vm":
            vm_min_avail = min(vm_min_avail or 10**12, r.get("vm_mem_available_kib", 10**12))
            offsets.append(r["clock_offset_ns"])
    return {"peak_container_mem_mib": {k: round(v, 1) for k, v in sorted(peak_mem.items())},
            "peak_container_cpu_pct": {k: round(v, 1) for k, v in sorted(peak_cpu.items())},
            "peak_total_container_mem_mib": round(sum(peak_mem.values()), 1),
            "host_min_memorystatus_free_pct": host_min_level,
            "vm_min_mem_available_mib": round(vm_min_avail / 1024, 1) if vm_min_avail else None,
            "clock_offset_ms_range": [round(min(offsets) / 1e6, 1), round(max(offsets) / 1e6, 1)] if offsets else None}


def case_summary(gt: dict, gt_dir: Path) -> dict:
    v, tc, wl = gt["validation"], gt["telemetry_check"], gt.get("workload_summary") or {}
    tl = gt["timeline_vm_ns"]
    return {
        "case_id": gt["case_id"], "scenario": gt["scenario"]["key"], "fault_type": gt["fault_type"],
        "target": gt["target"], "root_operation": gt["root_operation"], "case_valid": gt["case_valid"],
        "fault_occurred": v["fault_occurred"],
        "validator_checks_passed": f"{sum(c['passed'] for c in v['checks'])}/{len(v['checks'])}",
        "recovered": gt["recovery"]["recovered"], "observable_impact": gt["observable_impact"]["gateway_slo_deviated"],
        "propagated_symptom_services": gt["propagated_symptom_services"],
        "workload_by_window": {"baseline": gt["observable_impact"]["baseline"],
                               "injection_steady": gt["observable_impact"]["injection_steady"],
                               "recovery_last_30s": gt["recovery"]["recovery_late"]},
        "workload": {k: wl.get(k) for k in ("schedule_sha256", "offered_rate_rps", "achieved_rate_rps", "scheduled",
                                             "sent", "shed", "unexpected_outcome_rate", "p50_ms", "p95_ms",
                                             "status_counts")},
        "telemetry": {"passed": tc["passed"], "checks": tc["checks"], "records": tc["records"],
                      "throughput_per_s": tc["throughput_per_s"], "raw_mb": round(tc["raw_bytes"] / 1e6, 2),
                      "trace_completeness": tc["trace_completeness"], "unexpected_gaps": tc["unexpected_gaps"],
                      "expected_gap": tc["expected_gap"]},
        "durations_s": {"baseline": round((tl["injection_start_ns"] - tl["warmup_end_ns"]) / 1e9, 2),
                        "injection": round((tl["injection_end_ns"] - tl["injection_start_ns"]) / 1e9, 2),
                        "recovery": round((tl["recovery_end_ns"] - tl["injection_end_ns"]) / 1e9, 2),
                        "onset_vs_plan_ms": round((tl["injection_start_ns"] - tl["planned_injection_start_ns"]) / 1e6, 1)},
        "clock_offsets_vm_minus_host": gt["clock_offsets"], "monitor_errors": gt["monitor_errors"],
        "wall_clock_s": gt["wall_clock_s"], "resources": resources(gt_dir),
    }


def gt_validation_entry(gt: dict, group: str, label: str) -> dict:
    v = gt["validation"]
    return {
        "case_id": gt["case_id"], "group": group, "label": label, "scenario": gt["scenario"],
        "fault_type": gt["fault_type"], "primary_root_services": gt["primary_root_services"],
        "root_operation": gt["root_operation"], "params": gt["params"], "timeline_vm_ns": gt["timeline_vm_ns"],
        "injection_actions": gt["injection_actions"], "fault_occurred": v["fault_occurred"],
        "independent_checks": v["checks"], "signatures_during_injection": v["signatures_during_injection"],
        "detector_matrix": v["detector_matrix"], "recovery": gt["recovery"],
        "observable_impact": gt["observable_impact"]["gateway_slo_deviated"],
        "propagated_symptom_services": gt["propagated_symptom_services"],
        "propagation_analysis_only": gt["propagation_analysis_only"], "case_valid": gt["case_valid"],
    }


def repeatability(runs: dict) -> list[dict]:
    by_scenario = defaultdict(dict)
    for label, cases in runs.items():
        for c in cases:
            by_scenario[c["scenario"]][label] = c
    out = []
    for sc, per_label in sorted(by_scenario.items()):
        if len(per_label) < 2:
            continue
        (la, a), (lb, b) = sorted(per_label.items())[:2]

        def rel(x, y):
            return round(abs(x - y) / max(abs(x), abs(y), 1e-9), 3) if None not in (x, y) else None
        ia, ib = a["workload_by_window"]["injection_steady"], b["workload_by_window"]["injection_steady"]
        out.append({
            "scenario": sc, "labels": [la, lb], "case_ids": [a["case_id"], b["case_id"]],
            "identical_offered_schedule": a["workload"]["schedule_sha256"] == b["workload"]["schedule_sha256"],
            "same_validation_outcome": (a["fault_occurred"], a["validator_checks_passed"]) ==
                                       (b["fault_occurred"], b["validator_checks_passed"]),
            "same_recovery_outcome": a["recovered"] == b["recovered"],
            "same_observable_impact": a["observable_impact"] == b["observable_impact"],
            "same_propagated_symptoms": a["propagated_symptom_services"] == b["propagated_symptom_services"],
            "injection_unexpected_rate": [ia["unexpected_rate"], ib["unexpected_rate"]],
            "injection_p95_ms": [ia["p95_ms"], ib["p95_ms"]], "injection_p95_rel_diff": rel(ia["p95_ms"], ib["p95_ms"]),
            "span_count_rel_diff": rel(a["telemetry"]["records"]["spans"], b["telemetry"]["records"]["spans"]),
        })
    return out


def main() -> int:
    index = [json.loads(l) for l in (DATA_ROOT / "harness_index.jsonl").read_text().splitlines() if l.strip()]
    runs, mech, load, gtv = defaultdict(list), [], [], []
    for e in index:
        gt_dir = GT_ROOTS[e["group"]] / e["case_id"]
        gt = json.loads((gt_dir / "ground_truth.json").read_text())
        s = case_summary(gt, gt_dir)
        {"pilot": runs[e["label"]].append, "mechanism": mech.append, "load": load.append}[e["group"]](s)
        if e["group"] in ("pilot", "mechanism"):
            gtv.append(gt_validation_entry(gt, e["group"], e["label"]))

    # Mechanism checks: the latest run per scenario counts; earlier ones were development iterations.
    latest = {}
    for c in mech:
        latest[c["scenario"]] = c
    superseded = [{"case_id": c["case_id"], "scenario": c["scenario"], "case_valid": c["case_valid"],
                   "reason": "development iteration before a harness fix; rerun with the final harness"}
                  for c in mech if latest[c["scenario"]] is not c]
    mech = list(latest.values())
    kept = {c["case_id"] for c in mech} | {c["case_id"] for cs in runs.values() for c in cs}
    gtv = [g for g in gtv if g["case_id"] in kept]
    pilot_cases = [c for cs in runs.values() for c in cs]
    pilot = {
        "schema": "evidencerca.phase2.pilot/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": 11, "split": SPLITS[11], "protocol_timeline_s": vars(PROTOCOL_TIMELINE),
        "summary": {"pilot_cases": len(pilot_cases), "valid": sum(c["case_valid"] for c in pilot_cases),
                    "all_valid": all(c["case_valid"] for c in pilot_cases),
                    "mechanism_checks": len(mech), "mechanism_valid": sum(c["case_valid"] for c in mech),
                    "load_runs": len(load)},
        "pilot_runs": dict(runs), "repeatability": repeatability(runs),
        "mechanism_checks": {"timeline_s": vars(MECHANISM_CHECK_TIMELINE),
                             "note": "Short-timeline runs of every fault type to verify injector, independent "
                                     "validator and cleanup. Not experiment data.", "cases": mech,
                             "superseded_development_runs": superseded},
        "sustained_load": load,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "pilot_results.json").write_text(json.dumps(pilot, indent=2, default=str) + "\n")
    gt_out = {"schema": "evidencerca.phase2.ground_truth_validation/v1",
              "generated_at": pilot["generated_at"],
              "note": "Harness-side record. Contains labels; never readable by agent-side code "
                      "(evidencerca.rca_input reads data/cases only).",
              "all_occurred_and_recovered": all((c["fault_occurred"] in (True, None)) and c["recovery"]["recovered"]
                                                for c in gtv),
              "cases": gtv}
    (RESULTS / "ground_truth_validation.json").write_text(json.dumps(gt_out, indent=2, default=str) + "\n")
    print(json.dumps(pilot["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
