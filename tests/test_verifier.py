"""Verifier V0-V6 on synthetic evidence with known-bad claims (expected outcome known by construction)."""

import ast
import copy
from pathlib import Path

import pytest

from evidencerca.core import detect_alert
from evidencerca.evidence import View
from evidencerca.methods.evidencerca_e0 import diagnose
from evidencerca.verifier import verify, verify_claim
from tests.synth import make_view


@pytest.fixture(scope="module")
def latency_case():
    v = make_view("latency", "payment")
    d = diagnose(v, detect_alert(v))
    assert d["decision"] == "DIAGNOSE" and d["suspected_service"] == "payment"
    return v, d


def observed(d, predicate=None):
    return [c for c in d["factual_claims"] if c["kind"] == "observed" and (predicate is None or c["predicate"] == predicate)]


def test_genuine_claims_verify(latency_case):
    v, d = latency_case
    r = verify(d, v)
    assert r["summary"]["observed_verified"] >= 1 and not r["flags"]


def test_false_citation_fails_v1(latency_case):
    v, d = latency_case
    c = copy.deepcopy(observed(d)[0])
    c["evidence_ids"] = ["ev-" + "f" * 20]
    r = verify_claim(c, d, v)
    assert r["checks"]["V1"] == "fail" and not r["verified"]


def test_missing_record_fails_v1(latency_case):
    v, d = latency_case
    c = observed(d)[0]
    v2 = View(v.view_id, v.case_id, [x for x in v.records if x["evidence_id"] != c["evidence_ids"][0]])
    assert verify_claim(c, d, v2)["checks"]["V1"] == "fail"


def test_altered_value_fails_v4(latency_case):
    v, d = latency_case
    c = copy.deepcopy(observed(d, "latency_above_baseline")[0])
    c["value"] *= 3
    r = verify_claim(c, d, v)
    assert r["checks"]["V4"] == "fail" and not r["verified"]


def test_wrong_service_fails_v3(latency_case):
    v, d = latency_case
    c = copy.deepcopy(observed(d, "latency_above_baseline")[0])
    c["subject_service"] = "inventory"
    assert verify_claim(c, d, v)["checks"]["V3"] == "fail"


def test_wrong_view_fails_v2(latency_case):
    v, d = latency_case
    d2 = dict(d, view_id="vw-other")
    assert verify_claim(observed(d)[0], d2, v)["checks"]["V2"] == "fail"


def test_contradictory_evidence_fails_v5(latency_case):
    v, d = latency_case
    c = copy.deepcopy(observed(d, "latency_above_baseline")[0])
    w0, w1 = c["window"]
    normal = sorted((r for r in v.records if r["service"] == "payment" and r["kind"] == "SERVER"
                     and w0 <= r["ts_start"] < w1), key=lambda r: r["value"])[:3]
    c["evidence_ids"] = [r["evidence_id"] for r in normal]
    c["value"] = sorted(r["value"] for r in normal)[1]
    r = verify_claim(c, d, v)
    assert r["checks"]["V4"] == "pass" and r["checks"]["V5"] == "fail" and not r["verified"]


def test_fabricated_propagation_edge_fails_v6(latency_case):
    v, d = latency_case
    fake = copy.deepcopy(d)
    fake["propagation_path"] = [{"from": "payment", "to": "inventory", "evidence_ids": []}]   # inventory never calls payment
    assert verify(fake, v)["propagation"][0]["V6"] == "fail"


def test_real_propagation_edge_passes_v6(latency_case):
    v, d = latency_case
    assert d["propagation_path"] and all(p["V6"] == "pass" for p in verify(d, v)["propagation"])


def test_inferred_claim_is_never_a_verified_fact(latency_case):
    v, d = latency_case
    inferred = [c for c in d["factual_claims"] if c["kind"] == "inferred"]
    assert inferred
    r = verify_claim(inferred[0], d, v)
    assert r["premises_valid"] and not r["verified"]


def test_unknown_predicate_fails_schema(latency_case):
    v, d = latency_case
    c = dict(observed(d)[0], predicate="is_root_cause")
    assert verify_claim(c, d, v)["checks"]["V0"] == "fail"


def test_crash_no_telemetry_claim_verifies():
    v = make_view("crash", "inventory")
    d = diagnose(v, detect_alert(v))
    claims = [c for c in d["factual_claims"] if c["predicate"] == "no_telemetry_from"]
    assert claims and verify_claim(claims[0], d, v)["verified"]


def test_single_missing_record_is_not_no_telemetry():
    v = make_view("latency", "payment")
    d = diagnose(v, detect_alert(v))
    w0, w1 = d["windows"]["analysis"]
    recs = sorted((r for r in v.records if r["service"] == "order" and r["measurement"] == "container_cpu"
                   and w0 <= r["ts_start"] < w1), key=lambda r: r["ts_start"])
    v2 = View(v.view_id, v.case_id, [r for r in v.records if r["evidence_id"] != recs[len(recs) // 2]["evidence_id"]])
    c = {"claim_id": "x", "kind": "observed", "subject_service": "order", "object_service": None,
         "predicate": "no_telemetry_from", "measurement": "periodic_stream_gap", "value": 10.0, "unit": "s",
         "window": [w0, w1], "evidence_ids": [recs[0]["evidence_id"]]}
    r = verify_claim(c, d, v2)
    assert r["checks"]["V5"] == "fail" and not r["verified"]


def test_no_telemetry_requires_expected_stream_baseline():
    v = make_view("crash", "inventory")
    d = diagnose(v, detect_alert(v))
    w0, w1 = d["windows"]["analysis"]
    no_base = View(v.view_id, v.case_id, [r for r in v.records if not (r["service"] == "inventory" and
                                                                        r["modality"] == "metric" and r["ts_start"] < w0)])
    c = [c for c in d["factual_claims"] if c["predicate"] == "no_telemetry_from"][0]
    assert verify_claim(c, d, no_base)["checks"]["V5"] == "fail"


def test_verifier_is_independent_of_pipeline_and_labels():
    src = Path("evidencerca/verifier.py").read_text()
    imported = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and (m.startswith("evidencerca.") or m.startswith("harness") or m.startswith("evaluation"))
                   for m in imported), imported
    assert "ground_truth" not in src and "fault_type" not in src


def test_audit_sheet_is_blinded_and_unlabelled():
    from evaluation.audit_sample import build_sheet
    items = [{"claim": {"subject_service": "order", "predicate": "call_failed", "value": 3, "unit": "{call}",
                        "window": [0, 1]}, "cited_records": [], "context_records": [], "method": m,
              "verdict": v, "stratum": f"{m}|{v}"} for m in ("E0", "B4") for v in (True, False) for _ in range(60)]
    sheet, key = build_sheet(items, 200)
    assert len(sheet) == 200 and len(key) == 200
    for row in sheet:
        assert "method" not in row and "verifier_verdict" not in row and row["label"] is None
