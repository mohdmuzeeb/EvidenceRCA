"""Offline tests of the p4a-v2 typed schema and conditions (mock Ollama, no model call)
-> results/phase4a_v2/structured_output_tests_v2.json

  uv run python -m phase4.eval.structured_tests_v2
"""

import json
import sys
from pathlib import Path

from evaluation.run_dev import dev_cases
from evidencerca import rca_input
from evidencerca.core import detect_alert
from evidencerca.evidence import View, normalize
from evidencerca.methods.evidencerca_e0 import diagnose_full
from harness.leakage import scan_tokens
from phase4.agent import conditions_v2, evidence_pack, output_schema_v2 as schema, prompts_v2
from phase4.eval import pilot_v2
from phase4.eval.structured_tests import DEV_CASE_PREFIX, mock_client, ok_body

OUT = Path("results/phase4a_v2/structured_output_tests_v2.json")


def _pack(view, win, cond):
    first = None
    if cond == "E1":
        e0, _ = diagnose_full(view, win, use_graph=True)
        first = e0["evidence_ids"] + [x for e in e0["propagation_path"] for x in e["evidence_ids"]]
    return evidence_pack.build(view, win, first)


def base_answer(view, win, cond="B4") -> dict:
    """A typed answer: one latency claim per server span in the table (subject = emitter), one per call record
    (subject = callee), and the first observed call edge read backwards."""
    pk = _pack(view, win, cond)
    al = prompts_v2.aliases(pk)
    claims, edge = [], None
    for a, eid in al.items():
        r = view.by_id[eid]
        rt = schema.record_type(r)
        if rt == "server" and not any(c["evidence"][0]["record"] == "server" for c in claims):
            claims.append({"kind": "observed", "predicate": "latency_above_baseline", "subject_service": r["service"],
                           "object_service": "none", "measure": "duration", "value": r["value"], "unit": "ms",
                           "evidence": [{"id": a, "emitter": r["service"], "record": "server"}]})
        if rt == "call" and not any(c["evidence"][0]["record"] == "call" for c in claims):
            claims.append({"kind": "observed", "predicate": "latency_above_baseline", "subject_service": r["peer_service"],
                           "object_service": "none", "measure": "duration", "value": r["value"], "unit": "ms",
                           "evidence": [{"id": a, "emitter": r["service"], "record": "call"}]})
            edge = {"from": r["peer_service"], "to": r["service"], "evidence": [{"id": a, "emitter": r["service"], "record": "call"}]}
    return {"claims": claims, "propagation_path": [edge] if edge else [], "evidence_strength": "clear_anomaly",
            "uncertainty_reasons": [], "decision": "DIAGNOSE", "suspected_service": claims[0]["subject_service"],
            "suspected_fault_type": "processing_latency", "ranked_candidates": [claims[0]["subject_service"]],
            "explanation": "test"}


def scenarios(view, win) -> list[dict]:
    res = []

    def go(name, obj, expect, cond="B4", text=None, extra=lambda out: {}):
        cl, seen = mock_client([(200, ok_body(text if text is not None else json.dumps(obj)))])
        out = conditions_v2.run(cond, view, win, cl)
        tc = out.get("typed_checks") or {"claims": [], "edges": []}
        got = {"parse": out["parse"]["status"], "decision": out.get("decision"),
               "typed_claims_passed": [c["passed"] for c in tc["claims"]],
               "typed_edges_passed": [e["passed"] for e in tc["edges"]],
               "semantic_issues": out.get("semantic_issues"), "n_pre": len(out["claims"]["pre_filter"]),
               "n_post": len(out["claims"]["post_filter"]), **extra(out)}
        if seen:
            got["request_format_has_anyOf"] = "anyOf" in json.dumps(seen[0]["format"])
        ok = all(got.get(k) == v for k, v in expect.items())
        res.append({"name": name, "expected": expect, "observed": got, "passed": ok})
        return out

    good = base_answer(view, win)
    n = len(good["claims"])
    go("typed answer: server and call ownership, backwards edge", good,
       {"parse": "valid", "typed_claims_passed": [True] * n, "typed_edges_passed": [True] * len(good["propagation_path"]),
        "n_pre": n, "request_format_has_anyOf": True, "semantic_issues": []},
       extra=lambda out: {"edge_V6": [p["V6"] for p in out["verification"]["propagation"]]})
    for name, mut in [("count unit on latency", lambda o: o["claims"][0].update(unit="{call}")),
                      ("flag value unit '1' for call_failed", lambda o: o["claims"][0].update(
                          predicate="call_failed", measure="count", unit="1")),
                      ("measure class mismatch", lambda o: o["claims"][0].update(measure="count")),
                      ("object_service on a non-relational predicate", lambda o: o["claims"][0].update(object_service="order")),
                      ("citation without emitter", lambda o: o["claims"][0]["evidence"][0].pop("emitter")),
                      ("alias outside the table range", lambda o: o["claims"][0]["evidence"][0].update(id="E99")),
                      ("four citations", lambda o: o["claims"][0].update(evidence=o["claims"][0]["evidence"] * 4)),
                      ("six claims", lambda o: o.update(claims=o["claims"] * 6))]:
        o = json.loads(json.dumps(good))
        mut(o)
        go(f"schema: {name}", o, {"parse": "schema_noncompliant", "decision": "ABSTAIN", "n_pre": 0})
    o = json.loads(json.dumps(good))
    o["claims"][0]["evidence"][0]["emitter"] = next(s for s in ("gateway", "order", "payment")
                                                    if s != o["claims"][0]["evidence"][0]["emitter"])
    out = go("wrong declared emitter: typed-rejected, still verified and kept", o,
             {"parse": "valid", "n_pre": n, "c1_typed": False, "c1_in_post": False, "c1_verifier_ran": True},
             extra=lambda out: {"c1_typed": out["typed_checks"]["claims"][0]["passed"],
                                "c1_in_post": any(c["claim_id"] == "c1" for c in out["claims"]["post_filter"]),
                                "c1_verifier_ran": any(c["claim_id"] == "c1" for c in out["verification"]["claims"])})
    ci = next((i for i, c in enumerate(good["claims"]) if c["evidence"][0]["record"] == "call"), None)
    if ci is not None:
        o = json.loads(json.dumps(good))
        o["claims"][ci]["subject_service"] = o["claims"][ci]["evidence"][0]["emitter"]   # the caller
        go("call record used as evidence about the caller", o,
           {"parse": "valid", "caller_claim_typed": False},
           extra=lambda out: {"caller_claim_typed": out["typed_checks"]["claims"][ci]["passed"]})
    if good["propagation_path"]:
        o = json.loads(json.dumps(good))
        e = o["propagation_path"][0]
        e["from"], e["to"] = e["to"], e["from"]
        go("reversed propagation edge", o, {"parse": "valid", "typed_edges_passed": [False], "reversed_named": True},
           extra=lambda out: {"reversed_named": "reversed" in " ".join(out["typed_checks"]["edges"][0]["reasons"])})
        o = json.loads(json.dumps(good))
        o["propagation_path"][0].update({"from": "payment", "to": "postgres"})
        go("edge absent from the observed graph", o, {"parse": "valid", "typed_edges_passed": [False]})
    o = {**good, "claims": [], "propagation_path": [], "evidence_strength": "weak_or_no_deviation",
         "decision": "ABSTAIN", "suspected_service": "none", "suspected_fault_type": "unknown", "ranked_candidates": []}
    go("ABSTAIN with no suspected service", o, {"parse": "valid", "decision": "ABSTAIN", "semantic_issues": [], "n_pre": 0})
    o = {**good, "evidence_strength": "weak_or_no_deviation"}
    go("DIAGNOSE with weak evidence is flagged, not changed", o,
       {"parse": "valid", "decision": "DIAGNOSE", "semantic_issues": ["DIAGNOSE with evidence_strength weak_or_no_deviation"]})
    go("truncated output", good, {"parse": "malformed_json", "decision": "ABSTAIN", "n_pre": 0},
       text=json.dumps(good)[:200])
    e1 = base_answer(view, win, "E1")
    e1.update(decision="ABSTAIN", suspected_service="none", evidence_strength="weak_or_no_deviation")
    go("E1: LLM recommends ABSTAIN; E0's decision kept, recommendation recorded", e1,
       {"parse": "valid", "decision": "DIAGNOSE", "recommends_abstain": True, "e1_is_e0": True}, cond="E1",
       extra=lambda out: {"recommends_abstain": out["llm"]["recommends_abstain"],
                          "e1_is_e0": out["diagnosis"]["suspected_service"] == out["e0"]["suspected_service"]})
    return res


def leakage_scan() -> dict:
    """v2 messages for every pilot view and condition: label tokens, ground-truth strings, case ids."""
    plan = pilot_v2.make_plan()
    rows, systems = [], set()
    for row in plan["views"]:
        view, win, _ = pilot_v2.load_view(row)
        gt = next(c["gt"] for c in dev_cases() if c["case_id"] == row["case_id"])
        for cond in ("B4", "E1"):
            ranking = None
            if cond == "E1":
                ranking, _ = diagnose_full(view, win, use_graph=True)
                if ranking["decision"] != "DIAGNOSE":
                    continue
            pk = _pack(view, win, cond)
            msgs, _ = prompts_v2.build_messages(pk, cond, ranking)
            systems.add(msgs[0]["content"])
            text = msgs[1]["content"]
            scanned = "\n".join(l for l in text.splitlines() if not l.startswith("EvidenceRCA fault type:"))
            hits = scan_tokens(scanned.encode())
            gts = [gt["scenario"]["key"], str(gt["timeline_vm_ns"].get("injection_start_ns"))]
            rows.append({"view": row["index"], "condition": cond, "forbidden_token_hits": hits,
                         "gt_strings_present": [g for g in gts if g in text], "case_id_present": row["case_id"] in text})
    bad = [r for r in rows if r["forbidden_token_hits"] or r["gt_strings_present"] or r["case_id_present"]]
    return {"prompts_scanned": len(rows), "violations": bad, "distinct_system_prompts": len(systems),
            "passed": not bad and len(systems) == 1}


def main() -> int:
    cid = next(c["case_id"] for c in dev_cases() if c["case_id"].startswith(DEV_CASE_PREFIX))
    view = View("structured-test", cid, normalize(rca_input.load_case(cid)))
    win = detect_alert(view)
    scen = scenarios(view, win)
    leak = leakage_scan()
    rep = {"schema": "evidencerca.phase4a_v2.structured_output_tests/v1", "prompt_version": prompts_v2.PROMPT_VERSION,
           "schema_version": prompts_v2.SCHEMA_VERSION, "split": "dev seed 11 only", "model_calls": 0,
           "scenarios": scen, "prompt_leakage": leak, "passed": all(s["passed"] for s in scen) and leak["passed"]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    for s in scen:
        print("PASS" if s["passed"] else "FAIL", s["name"], "" if s["passed"] else s["observed"])
    print("prompt leakage:", "PASS" if leak["passed"] else "FAIL", leak["prompts_scanned"], "prompts",
          leak["violations"][:2])
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
