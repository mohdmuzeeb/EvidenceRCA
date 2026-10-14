"""Phase 4A-v2: p4a-v1 is preserved byte for byte; p4a-v2 is label-free and its typed checks behave (no model calls)."""

import hashlib
import json
from pathlib import Path

import pytest

from evaluation.freeze import verify_frozen
from harness import leakage
from phase4.agent import output_schema_v2 as schema, prompts, prompts_v2

REPO = Path(__file__).resolve().parent.parent


def test_frozen_tree_unchanged():
    verify_frozen()


def test_p4a_v1_preserved():
    man = json.loads((REPO / "results/phase4a_v2/p4a_v1_preserved.json").read_text())
    changed = [f for f, h in man["files"].items() if hashlib.sha256((REPO / f).read_bytes()).hexdigest() != h]
    assert not changed, changed
    bench = json.loads((REPO / "results/phase4a/five_call_benchmark.json").read_text())
    assert bench["prompt_version"] == prompts.PROMPT_VERSION == "p4a-v1"
    assert bench["calls"][0]["prompt_meta"]["system_sha256"] == hashlib.sha256(prompts.SYSTEM.encode()).hexdigest()


def test_v2_is_a_separate_version():
    assert prompts_v2.PROMPT_VERSION == "p4a-v2" and prompts_v2.SYSTEM != prompts.SYSTEM


def test_v2_agent_side_never_references_ground_truth(monkeypatch):
    monkeypatch.setattr(leakage, "AGENT_PACKAGES", ["phase4/agent"])
    res = leakage.check_static_imports(REPO)
    assert res["passed"], res["violations"]


def test_v2_system_prompt_label_free_apart_from_vocabulary():
    from evidencerca.methods.diagnosis import FAULT_TYPES
    vocab = " ".join(FAULT_TYPES) + " suspected_fault_type"
    hits = leakage.scan_tokens(prompts_v2.SYSTEM.encode())
    assert all(tok in vocab and n == 1 for tok, n in hits.items()), hits
    rest = "\n".join(l for l in prompts_v2.SYSTEM.splitlines() if "suspected_fault_type" not in l)
    assert leakage.scan_tokens(rest.encode()) == {}
    assert leakage.scan_tokens((prompts_v2.TASK_B4 + prompts_v2.TASK_E1).encode()) == {}


def test_v2_units_are_a_subset_of_the_frozen_verifier_units():
    from evidencerca.verifier import UNITS
    for pred, (_, units, _) in schema.TYPES.items():
        assert set(units) <= set(UNITS[pred]), pred


def _claim(**kw):
    c = {"kind": "observed", "predicate": "call_failed", "subject_service": "payment", "object_service": "none",
         "measure": "count", "value": 3, "unit": "{call}",
         "evidence": [{"id": "E01", "emitter": "order", "record": "call"}]}
    c.update(kw)
    return c


def _answer(claims, edges=()):
    return {"claims": claims, "propagation_path": list(edges), "evidence_strength": "clear_anomaly",
            "uncertainty_reasons": [], "decision": "DIAGNOSE", "suspected_service": "payment",
            "suspected_fault_type": "unknown", "ranked_candidates": ["payment"], "explanation": ""}


@pytest.mark.parametrize("kw", [{"unit": "1"}, {"measure": "duration"}, {"object_service": "order"},
                                {"evidence": []}, {"value": "3 calls"}])
def test_typed_schema_rejects(kw):
    assert schema.parse(json.dumps(_answer([_claim(**kw)])))["status"] == "schema_noncompliant"


def test_typed_schema_accepts_and_abstain_without_service():
    assert schema.parse(json.dumps(_answer([_claim()])))["status"] == "valid"
    o = _answer([]) | {"decision": "ABSTAIN", "suspected_service": "none", "ranked_candidates": [],
                       "evidence_strength": "weak_or_no_deviation"}
    assert schema.parse(json.dumps(o))["status"] == "valid" and schema.semantic_issues(o) == []


REC = {"r1": {"evidence_id": "r1", "modality": "trace", "kind": "CLIENT", "service": "order", "peer_service": "payment",
              "operation": "POST /pay", "status": "error", "error_type": "ReadTimeout", "measurement": "duration",
              "severity": None},
       "r2": {"evidence_id": "r2", "modality": "trace", "kind": "SERVER", "service": "payment", "peer_service": None,
              "operation": "POST /pay", "status": "ok", "error_type": None, "measurement": "duration", "severity": None}}
ALIAS = {"E01": "r1", "E02": "r2"}
GRAPH = {("order", "payment")}


def test_typed_checks_ownership_and_direction():
    ok = schema.typed_checks(_answer([_claim()], [{"from": "payment", "to": "order", "evidence": [
        {"id": "E01", "emitter": "order", "record": "call"}]}]), ALIAS, REC, GRAPH)
    assert all(c["passed"] for c in ok["claims"]) and all(e["passed"] for e in ok["edges"])
    bad = schema.typed_checks(_answer([
        _claim(subject_service="order"),                                                    # caller as subject
        _claim(evidence=[{"id": "E01", "emitter": "payment", "record": "call"}]),           # wrong emitter
        _claim(predicate="latency_above_baseline", measure="duration", unit="ms", subject_service="order",
               evidence=[{"id": "E02", "emitter": "payment", "record": "server"}]),         # another service's span
    ], [{"from": "order", "to": "payment", "evidence": [{"id": "E01", "emitter": "order", "record": "call"}]}]),
        ALIAS, REC, GRAPH)
    assert [c["passed"] for c in bad["claims"]] == [False, False, False]
    assert not bad["edges"][0]["passed"] and "reversed" in bad["edges"][0]["reasons"][0]


def test_structured_output_v2_report_passed():
    p = REPO / "results/phase4a_v2/structured_output_tests_v2.json"
    if not p.exists():
        pytest.skip("run `python -m phase4.eval.structured_tests_v2` first")
    rep = json.loads(p.read_text())
    assert rep["passed"] and rep["model_calls"] == 0 and rep["prompt_leakage"]["passed"]


def test_pilot_plan_matches_the_coded_rule():
    p = REPO / "results/phase4a_v2/pilot_plan.json"
    if not p.exists():
        pytest.skip("run `python -m phase4.eval.pilot_v2 plan` first")
    from phase4.eval.pilot_v2 import make_plan
    assert json.loads(p.read_text()) == json.loads(json.dumps(make_plan()))
