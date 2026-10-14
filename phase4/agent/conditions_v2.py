"""B4 and E1 under p4a-v2 (agent side). Same structure as phase4.agent.conditions (p4a-v1, unchanged):

B4  LLM-only; the model's own decision, suspect, ranking and fault type form the diagnosis.
E1  frozen E0 ranks and retrieves; the diagnosis-level outcome stays E0's. The model's own decision is recorded
    as its independent abstention recommendation, with any disagreement with E0, and never overrides E0.

New in v2: typed checks (phase4.agent.output_schema_v2.typed_checks) on agent-visible data. Claim sets:
  pre_filter            every claim the model wrote, verbatim
  typed_rejected_ids    claims rejected at typed validation (still verified, still kept)
  verifier_verified_ids observed claims the frozen verifier verified (it sees every claim, gate or not)
  post_filter           observed claims that pass typed validation AND the frozen verifier
"""

from evidencerca.abstention import AbstentionConfig, score
from evidencerca.methods.diagnosis import empty_diagnosis
from evidencerca.methods.evidencerca_e0 import diagnose_full
from evidencerca.verifier import verify
from phase4.agent import evidence_pack, output_schema_v2 as schema, prompts_v2 as prompts
from phase4.agent.conditions import NO_CALL


def _citations(obj, alias_map, records_by_id) -> dict:
    cites = [e for c in (obj or {}).get("claims") or [] for e in c.get("evidence", [])]
    known = [e for e in cites if e.get("id") in alias_map]
    own_ok = [e for e in known if records_by_id[alias_map[e["id"]]]["service"] == e.get("emitter")
              and schema.record_type(records_by_id[alias_map[e["id"]]]) == e.get("record")]
    return {"n_cited": len(cites), "n_known_alias": len(known), "n_ownership_correct": len(own_ok),
            "rate_known_alias": round(len(known) / len(cites), 4) if cites else None,
            "rate_ownership_correct": round(len(own_ok) / len(cites), 4) if cites else None}


def run(condition: str, view, win, client) -> dict:
    out = {"condition": condition, "prompt_version": prompts.PROMPT_VERSION, "view_id": view.view_id,
           "windows": win.as_dict(), "called": False}
    if not win.triggered:
        out.update(note=NO_CALL, decision="ABSTAIN")
        return out
    ranking, signals, first_ids = None, None, None
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
    records_by_id = {r["evidence_id"]: r for _, _, r in pk.records}
    graph_edges = {(a, b) for a, b, _ in pk.graph}
    messages, meta = prompts.build_messages(pk, condition, ranking)
    call = client.chat(messages, schema.SCHEMA, meta)
    out.update(called=True, prompt_meta=meta, alias_map=alias_map, call=call.as_dict())
    parsed = schema.parse(call.raw_text) if call.outcome == "ok" else \
        {"status": "no_response", "obj": None, "errors": [call.outcome]}
    out["parse"] = {"status": parsed["status"], "errors": parsed["errors"]}
    obj = parsed["obj"] if parsed["status"] == "valid" else None
    out["semantic_issues"] = schema.semantic_issues(obj) if obj else []
    out["citations"] = _citations(obj, alias_map, records_by_id)
    typed = schema.typed_checks(obj, alias_map, records_by_id, graph_edges) if obj else {"claims": [], "edges": []}
    out["typed_checks"] = typed
    if obj is None:
        diag = empty_diagnosis(view, condition, win)
        diag["uncertainty_reasons"].append(f"LLM output unusable: {parsed['status']}")
    else:
        diag = schema.to_diagnosis(obj, view, win, alias_map, condition)
        out["llm"] = {k: obj.get(k) for k in ("decision", "evidence_strength", "suspected_service",
                                               "suspected_fault_type", "ranked_candidates", "uncertainty_reasons",
                                               "explanation")}
        if condition == "B4":
            sus = obj["suspected_service"] if obj["suspected_service"] != "none" else None
            ranked = list(dict.fromkeys(([sus] if sus else []) + list(obj["ranked_candidates"])))
            diag["ranked_candidates"] = [{"service": s, "score": None} for s in ranked]
            diag["suspected_service"] = sus
            diag["suspected_fault_type"] = None if obj["suspected_fault_type"] == "unknown" else obj["suspected_fault_type"]
            diag["decision"] = "DIAGNOSE" if obj["decision"] == "DIAGNOSE" and sus else "ABSTAIN"
        else:
            for k in ("ranked_candidates", "suspected_service", "suspected_fault_type", "decision"):
                diag[k] = ranking[k]
            own = obj["suspected_service"]
            out["llm"]["agrees_with_e0_top1"] = own == ranking["suspected_service"]
            out["llm"]["recommends_abstain"] = obj["decision"] == "ABSTAIN"
            out["llm"]["names_other_service"] = own not in ("none", ranking["suspected_service"])
    ver = verify(diag, view) if diag["factual_claims"] or diag["propagation_path"] else None
    gate_ok = {f"c{c['index'] + 1}" for c in typed["claims"] if c["passed"]}
    ver_ok = {c["claim_id"] for c in (ver or {}).get("claims", []) if c["verified"]}
    edge_ok = {p["index"] for p in typed["edges"] if p["passed"]}
    out["diagnosis"] = diag
    out["decision"] = diag["decision"]
    out["verification"] = ver
    out["claims"] = {"pre_filter": diag["factual_claims"],
                     "typed_rejected_ids": [c["claim_id"] for c in diag["factual_claims"] if c["claim_id"] not in gate_ok],
                     "verifier_verified_ids": sorted(ver_ok),
                     "post_filter": [c for c in diag["factual_claims"] if c["claim_id"] in gate_ok & ver_ok],
                     "edges_post_filter": [e for i, (e, v) in enumerate(zip(diag["propagation_path"],
                                                                             (ver or {}).get("propagation", [])))
                                           if i in edge_ok and v["V6"] == "pass" and v["evidence"] == "pass"]}
    if condition == "E1":
        out["e1_abstention_descriptive"] = score(diag, signals, ver, view.unavailable, AbstentionConfig())
    return out
