"""Cross-service association for `onset_before` (verifier correction, protocol §13.2).

Hand-built views, independent of the pipeline: the expected verdict of every claim is known by construction.
"""

import pytest

from evidencerca.evidence import View, assign_ids
from evidencerca.verifier import _onset_side, verify_claim

SEC = 10**9
T0 = 1_900_000_000 * SEC
CASE = "00000000-0000-4000-8000-000000000001"


def rec(service, kind, t, value, peer=None, status="ok", modality="trace"):
    r = dict.fromkeys(("evidence_id", "case_id", "modality", "source", "service", "operation", "ts_start", "ts_end",
                       "measurement", "value", "unit", "trace_id", "span_id", "parent_span_id", "kind",
                       "peer_service", "status", "http_status", "error_type", "severity", "text"))
    r.update(modality=modality, source="otlp", service=service, operation="op", ts_start=t,
             ts_end=t + int(value * 1e6), measurement="span_duration", value=value, unit="ms", kind=kind,
             peer_service=peer, status=status, span_id=f"{t:016x}"[-16:])
    return r


@pytest.fixture
def scenario():
    """inventory becomes slow at T0+150 s; gateway's own server spans become slow at T0+160 s (beyond the 5 s V5 tolerance).
    Baseline 30-150 s: everything ~5 ms. Calls gateway -> inventory exist throughout."""
    recs = []
    for k in range(0, 300):
        t = T0 + k * SEC
        slow_inv = k >= 150
        slow_gw = k >= 160
        recs.append(rec("inventory", "SERVER", t, 500.0 if slow_inv else 5.0))
        recs.append(rec("gateway", "CLIENT", t + 1000, 501.0 if slow_inv else 5.5, peer="inventory"))
        recs.append(rec("gateway", "SERVER", t + 2000, 600.0 if slow_gw else 6.0))
    recs = assign_ids(recs, CASE)
    view = View("vw-onset", CASE, recs)
    diag = {"case_id": CASE, "view_id": "vw-onset", "decision": "DIAGNOSE"}
    w = [T0 + 120 * SEC, T0 + 240 * SEC]
    first = lambda svc, kind, k: next(r for r in recs if r["service"] == svc and r["kind"] == kind
                                      and r["ts_start"] >= T0 + k * SEC)
    return view, diag, w, first


def claim(subject, obj, value, unit, window, ids):
    return {"claim_id": "o", "kind": "observed", "subject_service": subject, "object_service": obj,
            "predicate": "onset_before", "measurement": None, "value": value, "unit": unit, "window": window,
            "evidence_ids": ids}


def test_association_rule():
    call_to_subject = rec("gateway", "CLIENT", T0, 5.0, peer="inventory")
    assert _onset_side(call_to_subject, "inventory", "gateway") == "subject"     # emitted by object, but a call to subject
    assert _onset_side(rec("gateway", "SERVER", T0, 5.0), "inventory", "gateway") == "object"
    assert _onset_side(rec("inventory", "SERVER", T0, 5.0), "inventory", "gateway") == "subject"
    assert _onset_side(rec("payment", "SERVER", T0, 5.0), "inventory", "gateway") is None


def test_valid_claim_in_seconds_verifies(scenario):
    view, diag, w, first = scenario
    a, b = first("inventory", "SERVER", 150), first("gateway", "SERVER", 160)
    lead = (b["ts_start"] - a["ts_start"]) / SEC
    r = verify_claim(claim("inventory", "gateway", lead, "s", w, [a["evidence_id"], b["evidence_id"]]), diag, view)
    assert r["verified"], r


def test_valid_claim_in_milliseconds_verifies(scenario):
    view, diag, w, first = scenario
    a, b = first("inventory", "SERVER", 150), first("gateway", "SERVER", 160)
    lead_ms = (b["ts_start"] - a["ts_start"]) / 1e6
    r = verify_claim(claim("inventory", "gateway", lead_ms, "ms", w, [a["evidence_id"], b["evidence_id"]]), diag, view)
    assert r["verified"], r


@pytest.mark.parametrize("scale_unit", [("s", 1000.0), ("ms", 0.001)])   # s value labelled as ms, ms value as s
def test_wrongly_scaled_unit_fails_v4(scenario, scale_unit):
    view, diag, w, first = scenario
    a, b = first("inventory", "SERVER", 150), first("gateway", "SERVER", 160)
    lead_s = (b["ts_start"] - a["ts_start"]) / SEC
    unit, _ = scale_unit
    wrong = lead_s if unit == "ms" else lead_s * 1000   # forgot to convert
    r = verify_claim(claim("inventory", "gateway", wrong, unit, w, [a["evidence_id"], b["evidence_id"]]), diag, view)
    assert r["checks"]["V4"] == "fail" and not r["verified"]


def test_wrong_lead_value_fails_v4(scenario):
    view, diag, w, first = scenario
    a, b = first("inventory", "SERVER", 150), first("gateway", "SERVER", 160)
    r = verify_claim(claim("inventory", "gateway", 30.0, "s", w, [a["evidence_id"], b["evidence_id"]]), diag, view)
    assert r["checks"]["V4"] == "fail"


def test_reversed_order_claim_fails_v5(scenario):
    view, diag, w, first = scenario
    a, b = first("gateway", "SERVER", 160), first("inventory", "SERVER", 150)
    lead = (b["ts_start"] - a["ts_start"]) / SEC     # negative: the claim's ordering is false
    r = verify_claim(claim("gateway", "inventory", lead, "s", w, [a["evidence_id"], b["evidence_id"]]), diag, view)
    assert r["checks"]["V5"] == "fail" and not r["verified"]


def test_call_to_subject_emitted_by_object_counts_for_subject(scenario):
    """The original-verifier case: subject's first anomaly evidenced by the object's CLIENT call to it."""
    view, diag, w, first = scenario
    a = first("gateway", "CLIENT", 150)          # gateway -> inventory call, slow because inventory is slow
    b = first("gateway", "SERVER", 160)
    lead = (b["ts_start"] - a["ts_start"]) / SEC
    r = verify_claim(claim("inventory", "gateway", lead, "s", w, [a["evidence_id"], b["evidence_id"]]), diag, view)
    assert r["checks"]["V3"] == "pass" and r["checks"]["V4"] == "pass", r


def test_client_span_referencing_object_but_not_emitted_by_it_is_not_object_evidence(scenario):
    """Object = inventory, cited record = gateway's CLIENT span to inventory (references inventory, emitted by gateway).
    With subject = gateway it is a record OF the subject (emitted by it), never of the object."""
    view, diag, w, first = scenario
    a = first("gateway", "SERVER", 160)
    c = first("gateway", "CLIENT", 150)
    r = verify_claim(claim("gateway", "inventory", 1.0, "s", w, [a["evidence_id"], c["evidence_id"]]), diag, view)
    assert r["checks"]["V3"] == "fail" and not r["verified"]


def test_record_of_unrelated_service_fails_v3(scenario):
    view, diag, w, first = scenario
    stray = assign_ids([rec("payment", "SERVER", T0 + 151 * SEC, 5.0)], CASE)[0]
    v2 = View(view.view_id, CASE, view.records + [stray])
    a = first("inventory", "SERVER", 150)
    r = verify_claim(claim("inventory", "gateway", 1.0, "s", w, [a["evidence_id"], stray["evidence_id"]]), diag, v2)
    assert r["checks"]["V3"] == "fail"
