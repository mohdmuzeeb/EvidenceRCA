"""Sensitivity analysis (not the primary result): test evaluation including one adjudicated case.

Scenario 41:network_timeout:inventory:0 failed the frozen validity rule on both attempts because of a deterministic
validator time-attribution artifact: one monitor sample whose read interval straddles the injection instant is
time-stamped at its midpoint, just before the recorded injection start, so `qdisc_netem` counts as "present before
injection". The independent TCP probe shows the fault present in 100 % of steady-injection probes and in 0 % before
and after. The primary evaluation (evaluation/run_final.py, frozen) excludes the scenario; this script re-runs the
same frozen functions with the latest attempt included, so the effect of that exclusion is visible.

  uv run python -m evaluation.sensitivity_adjudicated   # after `run_final evaluate`
"""

import hashlib
import json
from pathlib import Path

from evaluation import run_final as rf
from evaluation.freeze import verify_frozen
from evidencerca import paths

ADJUDICATED = {"case_prefix": "e6b597c8", "scenario": "41:network_timeout:inventory:0",
               "reason": "validator boundary artifact (qdisc sample straddling injection); independent TCP probe "
                         "confirms the fault: 100 % during injection, 0 % before and after"}


def main() -> int:
    verify_frozen()
    cal = json.loads(rf.CAL_FILE.read_text())
    cases = rf.cases_for("test")
    cid = next(p.name for p in paths.GROUND_TRUTH_ROOT.iterdir() if p.name.startswith(ADJUDICATED["case_prefix"]))
    gt = json.loads((paths.GROUND_TRUTH_ROOT / cid / "ground_truth.json").read_text())
    assert gt["scenario"]["key"] == ADJUDICATED["scenario"]
    cases.append({"case_id": cid, "role": "primary", "gt": gt})
    trials = rf.run_trials(cases)
    res = {"schema": "evidencerca.final.sensitivity_adjudicated/v1", "adjudicated_case": {**ADJUDICATED, "case_id": cid},
           "calibration_sha256": hashlib.sha256(rf.CAL_FILE.read_bytes()).hexdigest(), "n_cases": len(cases),
           "accuracy": rf.accuracy_tables(trials), "per_fault_type_C0": rf.per_fault(trials),
           "abstention": rf.abstention_tables(trials, cal), "paired_comparisons": rf.paired(trials)}
    Path("results/final/test_results_sensitivity_adjudicated.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    print("sensitivity written:", res["n_cases"], "cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
