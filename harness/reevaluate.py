"""Recompute validation, recovery, impact and telemetry checks of a recorded case from its stored files.

Used when analysis code changes after capture. Raw telemetry and harness records are only read; the
ground-truth file keeps a `reevaluations` log (time, reason, previous verdicts).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from harness.case import _jsonl
from harness.telemetry_check import check_case, load_raw
from harness.validate import capture_continuity, impact_and_recovery, propagated_symptoms, validate_case, windows


def reevaluate(case_id: str, cases_root: Path, gt_root: Path, reason: str) -> dict:
    gt_file = gt_root / case_id / "ground_truth.json"
    gt = json.loads(gt_file.read_text())
    hdir = gt_root / case_id / "harness"
    previous = {"case_valid": gt["case_valid"], "capture_continuity": gt.get("capture_continuity"), "fault_occurred": gt["validation"]["fault_occurred"],
                "recovered": gt["recovery"]["recovered"], "telemetry": gt["telemetry_check"]["passed"]}
    requests, probes, monitor = _jsonl(hdir / "requests.jsonl"), _jsonl(hdir / "probes.jsonl"), _jsonl(hdir / "monitor.jsonl")
    tel = cases_root / case_id / "telemetry"
    spans, logs, points, files = load_raw(tel)
    gt["validation"] = validate_case(gt, monitor, probes, requests, spans)
    gt.update(impact_and_recovery(gt, requests, monitor))
    gt["propagation_analysis_only"] = propagated_symptoms(spans, windows(gt["timeline_vm_ns"]),
                                                          gt["primary_root_services"])
    gt["propagated_symptom_services"] = gt["propagation_analysis_only"]["propagated_symptom_services"]
    gt["telemetry_check"] = check_case(gt, tel, (hdir / "collector_internal_metrics.prom").read_text(), requests,
                                       (hdir / "container_stdout.log").read_text(), spans, logs, points, files)
    gt["monitor_errors"] = sum(m["kind"] == "error" for m in monitor)
    gt["capture_continuity"] = capture_continuity(monitor, gt.get("workload_summary"), gt["workload_config"]["duration_s"])
    ok_fault = gt["validation"]["fault_occurred"] in (True, None) and all(c["passed"] for c in gt["validation"]["checks"])
    gt["case_valid"] = bool(ok_fault and gt["recovery"]["recovered"] and gt["telemetry_check"]["passed"]
                            and gt["injection_actions"]["error"] is None and gt["monitor_errors"] == 0
                            and gt["capture_continuity"]["passed"])
    gt.setdefault("reevaluations", []).append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                               "reason": reason, "previous": previous})
    gt_file.write_text(json.dumps(gt, indent=2, default=str) + "\n")
    return gt
