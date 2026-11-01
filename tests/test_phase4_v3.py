"""Phase 4A-v3: E2 deterministic ownership and the B4-G gate (no model calls)."""

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from evaluation.freeze import verify_frozen
from evidencerca.core import Windows
from phase4.agent import conditions_e2, ownership
from phase4.agent.llm_client import LLMSettings, OllamaClient
from tests.withheld import present

REPO = Path(__file__).resolve().parent.parent


def test_frozen_tree_unchanged():
    verify_frozen()


def test_phase4a_v1_and_v2_preserved():
    man = json.loads((REPO / "results/phase4a_v3/preserved_phase4a_v1_v2.json").read_text())
    changed = [f for f, h in man["files"].items() if present(f) and hashlib.sha256((REPO / f).read_bytes()).hexdigest() != h]
    assert not changed, changed


def _r(eid, modality="trace", kind="SERVER", service="payment", peer=None, op="POST /pay", meas="duration", unit="ms"):
    return {"evidence_id": eid, "modality": modality, "kind": kind, "service": service, "peer_service": peer,
            "operation": op, "measurement": meas, "unit": unit, "status": "ok", "error_type": None, "severity": None}


class _View:
    def __init__(self, recs):
        self.by_id = {r["evidence_id"]: r for r in recs}


V = _View([_r("srv"), _r("call", kind="CLIENT", service="order", peer="payment"),
           _r("call2", kind="CLIENT", service="gateway", peer="order"),
           _r("cpu", modality="metric", kind=None, meas="container_cpu", unit="%"),
           _r("conn", kind="CLIENT", service="inventory", op="connect")])


def _c(subject, ids, pred="latency_above_baseline", unit="ms", kind="observed", obj=None):
    return {"claim_id": "c1", "kind": kind, "subject_service": subject, "object_service": obj, "predicate": pred,
            "measurement": "duration", "value": 812.0, "unit": unit, "window": [0, 1], "evidence_ids": ids}


def test_consistent_claim_is_not_transformed():
    a = ownership.assess(_c("payment", ["srv", "call"]), V)
    assert a["disposition"] == "consistent" and "canonical" not in a


def test_caller_as_subject_gets_a_separate_canonical_candidate():
    orig = _c("order", ["call"])
    keep = copy.deepcopy(orig)
    a = ownership.assess(orig, V)
    assert orig == keep                                     # the original is never modified
    assert a["disposition"] == "transformed" and a["canonical"]["subject_service"] == "payment"
    assert a["canonical"]["claim_id"] == "c1~canon" and a["canonical"]["value"] == 812.0
    assert a["audit"]["changes"] == {"subject_service": ["order", "payment"]}
    assert a["cited_provenance"][0]["emitter"] == "order" and a["cited_provenance"][0]["callee"] == "payment"


def test_unit_fixed_by_family_without_rescaling():
    a = ownership.assess(_c("payment", ["call"], pred="call_failed", unit="1"), V)
    assert a["disposition"] == "transformed" and a["canonical"]["unit"] == "{call}" and a["canonical"]["value"] == 812.0


@pytest.mark.parametrize("claim,why", [
    (_c("payment", ["call", "call2"]), "different subjects"),
    (_c("payment", ["cpu"]), "not admissible"),
    (_c("inventory", ["conn"]), "not admissible"),
    (_c("payment", ["nope"]), "not in the view"),
])
def test_unresolvable_claims_are_rejected(claim, why):
    a = ownership.assess(claim, V)
    assert a["disposition"] == "rejected" and why in a["reason"] and "canonical" not in a


def test_stray_object_is_flagged_not_transformed():
    a = ownership.assess(_c("payment", ["srv"], obj="order"), V)
    assert a["disposition"] == "consistent" and a["stray_object_service"] == "order"


def test_onset_and_inferred_are_never_transformed():
    assert ownership.assess(_c("order", ["call"], pred="onset_before", obj="payment"), V)["disposition"] == "not_applicable"
    assert ownership.assess(_c("order", ["call"], kind="inferred"), V)["disposition"] == "not_applicable"


def test_canonicalize_keeps_diagnosis_untouched():
    diag = {"factual_claims": [_c("order", ["call"])]}
    keep = copy.deepcopy(diag)
    res = ownership.canonicalize(diag, V)
    assert diag == keep and len(res["canonical_claims"]) == 1


def test_b4g_abstains_without_calling_the_model(monkeypatch):
    sent = []

    def handler(req):
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "mock"})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        sent.append(req)
        return httpx.Response(500)
    client = OllamaClient(LLMSettings(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(conditions_e2, "detect_alert",
                        lambda view: Windows(False, "alert", None, None, None, None, "no SLO deviation detected"))

    class Vw:
        view_id = "v"
    out = conditions_e2.run_b4g(Vw(), client)
    assert out["decision"] == "ABSTAIN" and out["called"] is False and not sent and out["condition"] == "B4-G"


def test_plan_matches_the_coded_rule():
    p = REPO / "results/phase4a_v3/plan.json"
    if not p.exists():
        pytest.skip("run `python -m phase4.eval.pilot_v3 plan` first")
    from phase4.eval.pilot_v3 import make_plan
    assert json.loads(p.read_text()) == json.loads(json.dumps(make_plan()))
