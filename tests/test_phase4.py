"""Phase 4A: the LLM layer stays separate from the frozen study and from ground truth (no model calls)."""

import json
from pathlib import Path

import pytest

from evaluation.freeze import verify_frozen
from harness import leakage
from phase4.agent import output_schema, prompts

REPO = Path(__file__).resolve().parent.parent


def test_frozen_tree_unchanged():
    verify_frozen()


def test_phase4_agent_side_never_references_ground_truth(monkeypatch):
    monkeypatch.setattr(leakage, "AGENT_PACKAGES", ["phase4/agent"])
    res = leakage.check_static_imports(REPO)
    assert res["passed"], res["violations"]


def test_system_prompt_is_case_invariant_and_label_free_apart_from_vocabulary():
    # the only label-like tokens allowed in the instructions are the closed fault-type vocabulary
    from evidencerca.methods.diagnosis import FAULT_TYPES
    vocab = " ".join(FAULT_TYPES) + " suspected_fault_type"
    hits = leakage.scan_tokens(prompts.SYSTEM.encode())
    assert all(tok in vocab and n == 1 for tok, n in hits.items()), hits
    # with the vocabulary line and the key name removed, nothing remains
    rest = "\n".join(l for l in prompts.SYSTEM.splitlines() if "suspected_fault_type" not in l)
    assert leakage.scan_tokens(rest.encode()) == {}


def _good():
    return {"decision": "ABSTAIN", "suspected_service": "none", "suspected_fault_type": "unknown",
            "ranked_candidates": [], "claims": [], "propagation_path": [], "uncertainty_reasons": [], "explanation": ""}


def test_parse_valid_and_malformed():
    assert output_schema.parse(json.dumps(_good()))["status"] == "valid"
    assert output_schema.parse('{"decision": "DIAG')["status"] == "malformed_json"
    assert output_schema.parse("")["status"] == "malformed_json"
    assert output_schema.parse("[1, 2]")["status"] == "malformed_json"


@pytest.mark.parametrize("mut", [
    lambda o: o.pop("claims"),
    lambda o: o.update(decision="MAYBE"),
    lambda o: o.update(ranked_candidates=["gateway"] * 6),
    lambda o: o.update(extra=1),
    lambda o: o.update(claims=[{"kind": "observed", "predicate": "latency_above_baseline", "subject_service": "order",
                                "object_service": "none", "value": True, "unit": "ms", "evidence": ["E01"]}]),
])
def test_parse_schema_violations(mut):
    o = _good()
    mut(o)
    assert output_schema.parse(json.dumps(o))["status"] == "schema_noncompliant"


def test_semantic_issues_flag_unknown_alias_and_inconsistent_decision():
    o = _good() | {"decision": "DIAGNOSE", "claims": [{"kind": "observed", "predicate": "call_failed",
                                                        "subject_service": "order", "object_service": "none",
                                                        "value": 3, "unit": "{call}", "evidence": ["E07"]}]}
    issues = output_schema.semantic_issues(o, {"E01": "ev-x"})
    assert any("unknown aliases" in i for i in issues) and any("DIAGNOSE with" in i for i in issues)


def test_structured_output_report_passed():
    p = REPO / "results/phase4a/structured_output_tests.json"
    if not p.exists():
        pytest.skip("run `python -m phase4.eval.structured_tests` first")
    rep = json.loads(p.read_text())
    assert rep["passed"] and rep["model_calls"] == 0 and rep["prompt_leakage"]["passed"]
