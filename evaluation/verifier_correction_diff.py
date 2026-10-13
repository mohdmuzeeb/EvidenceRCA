"""Before/after record of the V4 `onset_before` association correction (protocol §13.2), dev data only.

Compares results/phase3/pre_verifier_correction/*.json (verifier as committed in 98f6a8e) with the current
results/phase3/*.json produced by the corrected verifier. -> results/phase3/verifier_correction_diff.json
"""

import json
from collections import Counter
from pathlib import Path

PRE, POST = Path("results/phase3/pre_verifier_correction"), Path("results/phase3")
KEY = ("case_id", "condition", "perturbation_seed", "anchor", "method")


def trials(p):
    return {tuple(t[k] for k in KEY): t for t in json.loads((p / "dev_results.json").read_text())["trials"]}


def main() -> int:
    a, b = trials(PRE), trials(POST)
    assert set(a) == set(b), "trial sets differ"
    ranking_changed = [k for k in a if a[k]["output_sha256"] != b[k]["output_sha256"]]
    claim_changes, per_pred, totals = [], Counter(), {"pre": Counter(), "post": Counter()}
    s_changed = 0
    for k in a:
        ta, tb = a[k], b[k]
        if ta["S"] != tb["S"]:
            s_changed += 1
        if not ta.get("claims"):
            continue
        for side, t in (("pre", ta), ("post", tb)):
            for c in t["claims"]:
                if c["kind"] == "observed":
                    totals[side]["observed"] += 1
                    totals[side]["verified"] += c["verified"]
        for ca, cb in zip(ta["claims"], tb["claims"]):
            if ca["kind"] == "observed" and ca["verified"] != cb["verified"]:
                per_pred[(ca["predicate"], "now_verified" if cb["verified"] else "now_rejected")] += 1
                if len(claim_changes) < 20:
                    claim_changes.append({"trial": dict(zip(KEY, k)), "predicate": ca["predicate"],
                                          "subject": ca["subject"], "before": ca["verified"], "after": cb["verified"]})
    va = json.loads((PRE / "verifier_tests.json").read_text())
    vb = json.loads((POST / "verifier_tests.json").read_text())
    aa = json.loads((PRE / "abstention_dev_results.json").read_text())
    ab = json.loads((POST / "abstention_dev_results.json").read_text())
    aurc = {m: {s: {"before": aa["by_method"][m][s]["aurc"], "after": ab["by_method"][m][s]["aurc"]}
                for s in aa["by_method"][m] if s != "controls"} for m in aa["by_method"]}
    out = {
        "schema": "evidencerca.phase3.verifier_correction_diff/v1",
        "correction": ("onset_before: a cited record counts towards the object only if the object emitted it and it is "
                       "not a call to the subject; otherwise towards the subject. Applied in V3 and V4."),
        "original_verifier": "results/phase3/pre_verifier_correction/verifier_v1.py (git 98f6a8e)",
        "trials_compared": len(a),
        "ranking_outputs_changed": len(ranking_changed),
        "trials_with_changed_abstention_S": s_changed,
        "e0_observed_claims": {"before": dict(totals["pre"]), "after": dict(totals["post"])},
        "claim_verdict_changes": {f"{p}:{d}": n for (p, d), n in sorted(per_pred.items())},
        "claim_change_examples": claim_changes,
        "mutation_tests": {k: {"before": va["mutations"][k]["rejection_rate"], "after": vb["mutations"][k]["rejection_rate"],
                               "by_expected_check_before": va["mutations"][k]["rejected_by_expected_check"],
                               "by_expected_check_after": vb["mutations"][k]["rejected_by_expected_check"]}
                           for k in va["mutations"]},
        "positive_control": {"before": va["positive_control_unmodified_observed_claims"],
                             "after": vb["positive_control_unmodified_observed_claims"]},
        "aurc": aurc,
    }
    (POST / "verifier_correction_diff.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({k: out[k] for k in ("ranking_outputs_changed", "trials_with_changed_abstention_S",
                                          "e0_observed_claims", "claim_verdict_changes", "positive_control")}, indent=1))
    print(json.dumps(out["aurc"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
