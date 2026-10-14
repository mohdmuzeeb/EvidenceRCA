"""E2 (post-study, separate from E1) and B4-G (gated B4). Agent side.

E2  = E1 under p4a-v1 (frozen E0 ranking/localization, E0-retrieved evidence, the same LLM call), followed by
      deterministic evidence ownership (phase4.agent.ownership). The model's original claims are kept verbatim
      and verified by the frozen verifier exactly as in E1; canonical candidates derived from provenance are
      verified separately by the same frozen verifier and are never counted as model claims.
B4-G = the frozen shared detector (evidencerca.core.detect_alert) as a pre-diagnosis gate in front of B4:
      no alert -> ABSTAIN without an LLM call; alert -> the original p4a-v1 B4 prompt and evidence pack,
      unchanged. B4-G is not an LLM-only baseline: its abstention comes from a deterministic detector.
"""

from evidencerca.core import detect_alert
from evidencerca.verifier import verify
from phase4.agent import conditions, ownership

GATE_NO_CALL = "B4-G: the frozen detector did not alert; ABSTAIN without an LLM call"


def e2_from_e1(e1: dict, view) -> dict:
    """E2 output from a completed E1 (p4a-v1) output on the same view. Does not modify `e1`."""
    out = {k: v for k, v in e1.items() if k not in ("condition",)}
    out["condition"] = "E2"
    out["derived_from"] = {"condition": "E1", "prompt_version": (e1.get("prompt_meta") or {}).get("prompt_version")}
    diag = e1.get("diagnosis")
    if not e1.get("called") or diag is None:
        out["ownership"] = None
        return out
    own = ownership.canonicalize(diag, view)
    canon_diag = dict(diag, factual_claims=own["canonical_claims"], propagation_path=[])
    canon_ver = verify(canon_diag, view) if own["canonical_claims"] else None
    ok_orig = {c["claim_id"] for c in (e1.get("verification") or {}).get("claims", []) if c["verified"]}
    ok_canon = {c["claim_id"] for c in (canon_ver or {}).get("claims", []) if c["verified"]}
    out["ownership"] = own
    out["canonical_verification"] = canon_ver
    out["claims"] = {"original_pre_filter": diag["factual_claims"],
                     "original_verified": [c for c in diag["factual_claims"] if c["claim_id"] in ok_orig],
                     "canonical_candidates": own["canonical_claims"],
                     "canonical_verified": [c for c in own["canonical_claims"] if c["claim_id"] in ok_canon],
                     "rejected_ids": [a["claim_id"] for a in own["assessments"] if a["disposition"] == "rejected"]}
    return out


def run_e2(view, win, client) -> dict:
    e1 = conditions.run("E1", view, win, client)
    return e2_from_e1(e1, view)


def run_b4g(view, client) -> dict:
    """Gate on the frozen detector, then the unchanged B4 (p4a-v1) on the detector's own window."""
    win = detect_alert(view)
    if not win.triggered:
        return {"condition": "B4-G", "view_id": view.view_id, "gate": {"alert": False, "reason": win.reason},
                "called": False, "note": GATE_NO_CALL, "decision": "ABSTAIN"}
    out = conditions.run("B4", view, win, client)
    out.update(condition="B4-G", gate={"alert": True, "anchor": win.anchor})
    return out
