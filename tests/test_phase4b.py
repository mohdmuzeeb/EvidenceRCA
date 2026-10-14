"""Phase 4B: locked plan, request hashes, scoring rules and audit sampling (no model calls)."""

import hashlib
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from evaluation.freeze import verify_frozen
from phase4.agent import output_schema
from phase4.agent.llm_client import LLMSettings, OllamaClient
from phase4.eval import phase4b

REPO = Path(__file__).resolve().parent.parent
PLAN = REPO / "results/phase4b/plan.json"


def test_frozen_tree_unchanged():
    verify_frozen()


def test_pre_phase4b_artifacts_preserved():
    man = json.loads((REPO / "results/phase4b/preserved_before_phase4b.json").read_text())
    changed = [f for f, h in man["files"].items() if hashlib.sha256((REPO / f).read_bytes()).hexdigest() != h]
    assert not changed, changed


def test_request_hash_equals_the_clients():
    seen = {}

    def handler(req):
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "mock"})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b", "digest": "d"}]})
        seen["body"] = req.content
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})
    client = OllamaClient(LLMSettings(), transport=httpx.MockTransport(handler))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    rec = client.chat(msgs, output_schema.SCHEMA, {})
    assert rec.request_sha256 == phase4b.request_sha256(msgs) == hashlib.sha256(seen["body"]).hexdigest()


def _plan():
    if not PLAN.exists():
        pytest.skip("run `python -m phase4.eval.phase4b plan` first")
    return json.loads(PLAN.read_text())


def test_plan_invariants():
    p = _plan()
    assert (p["n_cases"], p["n_fault_cases"], p["n_control_cases"], p["n_views"]) == (53, 47, 6, 265)
    assert p["prompt_version"] == "p4a-v1" and p["leakage"]["passed"] and p["equal_record_budget_B4_E1"]
    assert p["e0_reproduces_frozen_on_alerted_views"][0] == p["e0_reproduces_frozen_on_alerted_views"][1]
    by_view = Counter((c["view_index"], c["condition"]) for c in p["calls"])
    for v in p["views"]:
        assert by_view[(v["index"], "B4")] == 1                                    # B4 is ungated
        assert by_view[(v["index"], "E1")] == (v["e0_decision"] == "DIAGNOSE")    # E1 iff E0 has a candidate
        assert v["order"] == (["B4", "E1"] if v["index"] % 2 == 0 else ["E1", "B4"])
    assert [c["k"] for c in p["calls"]] == list(range(len(p["calls"])))
    assert len({c["system_sha256"] for c in p["calls"]}) == 1


def test_first_view_requests_rebuild_identically():
    p = _plan()
    row = p["views"][0]
    gt = next(c["gt"] for c in phase4b.test_cases() if c["case_id"] == row["case_id"])
    view, win = phase4b._load(row, gt)
    from evidencerca.methods.evidencerca_e0 import diagnose_full
    from phase4.agent import evidence_pack, prompts
    e0, _ = diagnose_full(view, win, use_graph=True)
    for c in (c for c in p["calls"] if c["view_index"] == 0):
        ranking = e0 if c["condition"] == "E1" else None
        pk = evidence_pack.build(view, win, phase4b._first_ids(e0) if ranking else None)
        msgs, _ = prompts.build_messages(pk, c["condition"], ranking)
        assert phase4b.request_sha256(msgs) == c["request_sha256"]


def test_e1_diagnosis_is_e0s_even_when_llm_output_unusable():
    row = {"index": 0, "case_id": "c", "scenario_key": "s", "seed": 41, "spec": "C0", "alert_fired": True}
    gt = {"primary_root_services": ["payment"], "fault_type": "service_crash"}
    e0 = {"decision": "DIAGNOSE", "suspected_service": "payment", "suspected_fault_type": "service_crash",
          "ranked_candidates": [{"service": "payment", "score": 0.9}], "propagation_path": []}
    out = {"called": True, "decision": "ABSTAIN", "e0": e0, "parse": {"status": "malformed_json"},
           "diagnosis": {"decision": "ABSTAIN", "suspected_service": None, "ranked_candidates": []}}
    t = phase4b._trial("E1", row, gt, out)
    assert t["decision"] == "DIAGNOSE" and t["top1_correct"] and t["fault_type_correct"]
    b4 = phase4b._trial("B4", row, gt, out)
    assert b4["decision"] == "ABSTAIN" and b4["top1_correct"] is False


def test_audit_picker_caps_per_base_case_and_covers_groups():
    import random
    pool = [{"k": i, "claim_id": f"c{j}", "case_id": f"case{i % 5}", "kind": "observed",
             "predicate": ["latency_above_baseline", "call_failed", "onset_before", "no_telemetry_from"][j % 4]}
            for i in range(20) for j in range(4)]
    used = Counter()
    got = phase4b._pick_capped(pool, 15, random.Random(1), used)
    assert len(got) == 15 and max(Counter(c["case_id"] for c in got).values()) <= phase4b.AUDIT_MAX_PER_CASE
    assert len({c["predicate"] for c in got}) == 4
