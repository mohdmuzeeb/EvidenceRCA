"""The two LLM conditions (agent side; inputs are a View and the shared alert windows only).

B4  LLM-only: the model sees the method-neutral evidence pack and produces the whole diagnosis (decision,
    suspected service, ranking, fault type, claims, propagation path).
E1  post-study EvidenceRCA + LLM: frozen E0 ranks candidates and retrieves evidence; the model sees the same
    pack (record table led by E0's retrieved records, same size) plus E0's ranking and writes the claims and
    explanation. The diagnosis-level outcome (decision, ranking, suspected service and fault type) stays E0's;
    the model's own choice is kept as `llm_*` fields and never overrides it.

Both: the frozen independent verifier checks every generated claim. Pre-filter claims (all of them) and the
post-filter set (verified observed claims) are both returned; rejected claims are never rewritten. The frozen
abstention score is computed but not thresholded here (tau was calibrated for E0 only).
"""

from evidencerca.abstention import AbstentionConfig, score
from evidencerca.methods.diagnosis import empty_diagnosis
from evidencerca.methods.evidencerca_e0 import diagnose_full
from evidencerca.verifier import verify
from phase4.agent import evidence_pack, output_schema, prompts

NO_CALL = "no LLM call: the shared detector did not alert, so no analysis window exists"


def _citations(obj: dict | None, alias_map: dict, view) -> dict:
    cites = [e for c in (obj or {}).get("claims") or [] for e in c.get("evidence", [])]
    known = [e for e in cites if e in alias_map]
    return {"n_cited": len(cites), "n_known_alias": len(known),
            "n_in_view": sum(alias_map[e] in view.by_id for e in known),
            "rate_known_alias": round(len(known) / len(cites), 4) if cites else None}


def _claim_sets(diag: dict, ver: dict | None) -> dict:
    ok = {c["claim_id"] for c in (ver or {}).get("claims", []) if c["verified"]}
    return {"pre_filter": diag["factual_claims"],
            "post_filter": [c for c in diag["factual_claims"] if c["claim_id"] in ok],
            "rejected_ids": (ver or {}).get("rejected_claim_ids", [])}


def run(condition: str, view, win, client) -> dict:
    out = {"condition": condition, "view_id": view.view_id, "windows": win.as_dict(), "called": False}
    if not win.triggered:
        out.update(note=NO_CALL, decision="ABSTAIN")
        return out
    ranking, signals = None, None
    first_ids = None
    if condition == "E1":
        e0, signals = diagnose_full(view, win, use_graph=True)
        e0_ver = verify(e0, view) if e0["decision"] == "DIAGNOSE" else None
        out["e0"] = {k: e0[k] for k in ("decision", "suspected_service", "suspected_fault_type", "ranked_candidates",
                                        "propagation_path")}
        out["e0_abstention"] = score(e0, signals, e0_ver, view.unavailable, AbstentionConfig())
        if e0["decision"] != "DIAGNOSE":
            out.update(note="no LLM call: E0 has no candidate with a positive score", decision="ABSTAIN")
            return out
        ranking = e0
        first_ids = e0["evidence_ids"] + [x for e in e0["propagation_path"] for x in e["evidence_ids"]]
    pk = evidence_pack.build(view, win, first_ids)
    alias_map = prompts.aliases(pk)
    messages, meta = prompts.build_messages(pk, condition, ranking)
    call = client.chat(messages, output_schema.SCHEMA, meta)
    out.update(called=True, prompt_meta=meta, alias_map=alias_map, call=call.as_dict())
    parsed = output_schema.parse(call.raw_text) if call.outcome == "ok" else \
        {"status": "no_response", "obj": None, "errors": [call.outcome]}
    out["parse"] = {"status": parsed["status"], "errors": parsed["errors"]}
    obj = parsed["obj"] if parsed["status"] == "valid" else None
    out["semantic_issues"] = output_schema.semantic_issues(obj, alias_map) if obj else []
    out["citations"] = _citations(obj, alias_map, view)
    if obj is None:
        diag = empty_diagnosis(view, condition, win)
        diag["uncertainty_reasons"].append(f"LLM output unusable: {parsed['status']}")
    else:
        diag = output_schema.to_diagnosis(obj, view, win, alias_map, condition)
        out["llm"] = {k: obj.get(k) for k in ("decision", "suspected_service", "suspected_fault_type",
                                               "ranked_candidates", "uncertainty_reasons", "explanation")}
        if condition == "B4":
            sus = obj["suspected_service"] if obj["suspected_service"] != "none" else None
            ranked = list(dict.fromkeys(([sus] if sus else []) + list(obj["ranked_candidates"])))
            # rank positions only; B4 has no scores, so no margin is defined for it
            diag["ranked_candidates"] = [{"service": s, "score": None} for s in ranked]
            diag["suspected_service"] = sus
            diag["suspected_fault_type"] = None if obj["suspected_fault_type"] == "unknown" else obj["suspected_fault_type"]
            diag["decision"] = "DIAGNOSE" if obj["decision"] == "DIAGNOSE" and sus else "ABSTAIN"
        else:
            for k in ("ranked_candidates", "suspected_service", "suspected_fault_type", "decision"):
                diag[k] = ranking[k]
            out["llm"]["agrees_with_e0_top1"] = obj["suspected_service"] == ranking["suspected_service"]
    ver = verify(diag, view) if diag["factual_claims"] or diag["propagation_path"] else None
    out["diagnosis"] = diag
    out["decision"] = diag["decision"]
    out["verification"] = ver
    out["claims"] = _claim_sets(diag, ver)
    if condition == "E1":
        out["e1_abstention_descriptive"] = score(diag, signals, ver, view.unavailable, AbstentionConfig())
    return out
