"""Verifier evaluation by construction (dev seed 11 only) -> results/phase3/verifier_tests.json.

The verifier's own verdicts are never used as ground truth. Each test takes a real EvidenceRCA claim from a
dev case and corrupts it in a known way; the correct outcome (reject, and by which check) is known by
construction. Unmodified claims are reported as the positive control. The human audit (>= 200 claims) is the
independent check of the verifier's *precision on real claims*; it is specified in
docs/evidence_verifier_spec.md and is not performed here.

  uv run python -m evaluation.verifier_tests
"""

import copy
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evaluation.run_dev import dev_cases
from evaluation.views import build
from evidencerca import rca_input
from evidencerca.core import detect_alert
from evidencerca.evidence import ALL_SERVICES, View, normalize
from evidencerca.methods.evidencerca_e0 import diagnose
from evidencerca.verifier import call_edges, verify, verify_claim

OUT = Path("results/phase3/verifier_tests.json")
EXPECT = {  # mutation -> the check that must fail
    "false_citation": "V1", "missing_record": "V1", "window_shift": "V2", "wrong_service": "V3",
    "altered_value": "V4", "unit_swap": "V4", "contradictory_evidence": "V5",
}


def mutate(kind, cl, view, diag):
    """Return (mutated claim, view to verify against) or None if the mutation does not apply."""
    c = copy.deepcopy(cl)
    if kind == "false_citation":
        c["evidence_ids"][0] = "ev-" + "0" * 20
        return c, view
    if kind == "missing_record":
        gone = c["evidence_ids"][0]
        return c, View(view.view_id, view.case_id, [r for r in view.records if r["evidence_id"] != gone],
                       view.unavailable, view.time_tolerance_ns)
    if kind == "window_shift":
        span = c["window"][1] - c["window"][0]
        c["window"] = [view.t_min - 3 * span, view.t_min - 2 * span]
        return c, view
    if kind == "wrong_service":
        others = [s for s in ALL_SERVICES if s not in (c["subject_service"], c.get("object_service"))]
        c["subject_service"] = sorted(others)[0]
        return c, view
    if kind == "altered_value":
        c["value"] = (c["value"] + 10) if c["predicate"] == "onset_before" else c["value"] * 3 + 1
        return c, view
    if kind == "unit_swap":
        swap = {"ms": "s", "s": "ms", "1": "%", "%": "1"}
        if c["unit"] not in swap or c["predicate"] in ("cpu_above_baseline",):   # cpu accepts both % and 1 as-is
            return None
        c["unit"] = swap[c["unit"]]
        return c, view
    if kind == "contradictory_evidence":
        w0, w1 = c["window"]
        p = c["predicate"]
        cited = [view.by_id[e] for e in c["evidence_ids"] if e in view.by_id]
        if not cited:
            return None
        fam = (cited[0]["modality"], cited[0]["measurement"], cited[0]["kind"], cited[0]["service"],
               cited[0]["peer_service"])
        pool = [r for r in view.records if w0 <= r["ts_start"] < w1 and
                (r["modality"], r["measurement"], r["kind"], r["service"], r["peer_service"]) == fam]
        if p in ("latency_above_baseline", "cpu_above_baseline", "pool_wait_above_baseline"):
            pool.sort(key=lambda r: (r["value"], r["evidence_id"]))
            normal = pool[:len(c["evidence_ids"])]                       # the lowest values in the same family
            if not normal:
                return None
            c["evidence_ids"] = [r["evidence_id"] for r in normal]
            vals = sorted(r["value"] for r in normal)
            c["value"] = vals[len(vals) // 2]                             # value consistent with the new citations
            return c, view
        if p in ("call_failed", "timeout_observed", "error_rate_above_baseline"):
            ok = [r for r in pool if r["status"] == "ok"][:len(c["evidence_ids"])]
            if not ok:
                return None
            c["evidence_ids"] = [r["evidence_id"] for r in ok]
            return c, view
        return None
    raise ValueError(kind)


def no_telemetry_single_gap(view, diag):
    """A healthy service with one periodic record removed must NOT verify as `no_telemetry_from`."""
    w0, w1 = diag["windows"]["analysis"]
    out = []
    for svc in ALL_SERVICES:
        if svc == diag.get("suspected_service"):
            continue
        recs = sorted((r for r in view.records if r["service"] == svc and r["measurement"] == "container_cpu"
                       and w0 <= r["ts_start"] < w1), key=lambda r: r["ts_start"])
        if len(recs) < 3:
            continue
        gone = recs[len(recs) // 2]
        v2 = View(view.view_id, view.case_id, [r for r in view.records if r["evidence_id"] != gone["evidence_id"]],
                  view.unavailable, view.time_tolerance_ns)
        cl = {"claim_id": "t", "kind": "observed", "subject_service": svc, "object_service": None,
              "predicate": "no_telemetry_from", "measurement": "periodic_stream_gap", "value": 10.0, "unit": "s",
              "window": [w0, w1], "evidence_ids": [recs[0]["evidence_id"]]}
        r = verify_claim(cl, diag, v2)
        out.append({"service": svc, "verified": r["verified"], "V5": r["checks"].get("V5")})
    return out


def main() -> int:
    results = defaultdict(lambda: {"n": 0, "rejected": 0, "rejected_by_expected_check": 0, "examples": []})
    positive = Counter()
    causation = Counter()
    edge_tests = Counter()
    single_gap = []
    per_case = []
    for case in dev_cases():
        if case["role"] != "primary" or case["gt"]["fault_type"] == "none":
            continue
        clean = normalize(rca_input.load_case(case["case_id"]))
        view, _ = build(clean, case["case_id"], "C0", 1, case["gt"])
        win = detect_alert(view)
        if not win.triggered:
            per_case.append({"case_id": case["case_id"], "skipped": "no alert"})
            continue
        diag = diagnose(view, win)
        if diag["decision"] != "DIAGNOSE":
            continue
        base = verify(diag, view)
        per_case.append({"case_id": case["case_id"], **base["summary"]})
        for cr in base["claims"]:
            if cr["kind"] == "observed":
                positive["verified" if cr["verified"] else "rejected"] += 1
            else:
                causation["inferred_claims"] += 1
                causation["premises_valid"] += cr.get("premises_valid", False)
                causation["counted_as_verified_fact"] += cr["verified"]
        for cl in diag["factual_claims"]:
            if cl["kind"] != "observed":
                continue
            for kind, check in EXPECT.items():
                m = mutate(kind, cl, view, diag)
                if m is None:
                    continue
                mc, mv = m
                r = verify_claim(mc, diag, mv)
                e = results[kind]
                e["n"] += 1
                e["rejected"] += not r["verified"]
                e["rejected_by_expected_check"] += r["checks"].get(check) == "fail"
                if r["verified"] and len(e["examples"]) < 3:
                    e["examples"].append({"case_id": case["case_id"], "predicate": cl["predicate"], "checks": r["checks"]})
        # fabricated propagation edge: from the root to a service that never calls it
        edges = call_edges(view)
        root = diag["suspected_service"]
        for s in ALL_SERVICES:
            if s != root and (s, root) not in edges:
                fake = copy.deepcopy(diag)
                fake["propagation_path"] = [{"from": root, "to": s, "evidence_ids": []}]
                res = verify(fake, view)["propagation"][0]
                edge_tests["n"] += 1
                edge_tests["rejected_V6"] += res["V6"] == "fail"
        for e in diag["propagation_path"]:
            edge_tests["real_edges"] += 1
            edge_tests["real_edges_pass_V6"] += any(p["edge"] == f"{e['from']}->{e['to']}" and p["V6"] == "pass"
                                                     for p in base["propagation"])
        single_gap.extend(no_telemetry_single_gap(view, diag))

    unit = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_verifier.py"], capture_output=True,
                          text=True)
    report = {
        "schema": "evidencerca.phase3.verifier_tests/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "split": "dev (seed 11) only",
        "method": "mutation of real E0 claims; expected outcome known by construction (not the verifier's own verdict)",
        "positive_control_unmodified_observed_claims": dict(positive),
        "mutations": {k: {**v, "rejection_rate": round(v["rejected"] / v["n"], 4) if v["n"] else None,
                          "expected_check": EXPECT[k]} for k, v in results.items()},
        "fabricated_propagation_edges": dict(edge_tests),
        "valid_citation_is_not_causation": dict(causation),
        "no_telemetry_from_single_missing_record": {"n": len(single_gap),
                                                     "wrongly_verified": sum(x["verified"] for x in single_gap),
                                                     "results": single_gap},
        "per_case_native_verification": per_case,
        "unit_tests": {"command": "pytest -q tests/test_verifier.py", "returncode": unit.returncode,
                       "summary": unit.stdout.strip().splitlines()[-1] if unit.stdout else unit.stderr[-300:]},
        "human_audit": "specified in docs/evidence_verifier_spec.md (>= 200 claims, blinded); NOT yet performed",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(json.dumps({k: report[k] for k in ("positive_control_unmodified_observed_claims",
                                             "fabricated_propagation_edges", "valid_citation_is_not_causation")}))
    for k, v in report["mutations"].items():
        print(k, v["n"], v["rejection_rate"], v["rejected_by_expected_check"])
    print("single-gap no_telemetry wrongly verified:", report["no_telemetry_from_single_missing_record"]["wrongly_verified"])
    print("unit tests:", report["unit_tests"]["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
