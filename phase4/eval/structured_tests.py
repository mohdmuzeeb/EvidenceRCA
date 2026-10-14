"""Offline structured-output and integration tests (no model call): -> results/phase4a/structured_output_tests.json

A mock HTTP transport stands in for Ollama, so parsing, validation, retries, timeouts, alias mapping, the frozen
verifier hand-off and the pre/post-filter claim sets are exercised deterministically. Also checks that the
case-dependent prompt text carries no label tokens on every dev case (seed 11) and view used, and that the
instruction text is identical across cases.

  uv run python -m phase4.eval.structured_tests
Evaluation side: reads dev ground truth only to build views (evaluation.views) and to pick the dev cases.
"""

import json
import sys
from pathlib import Path

import httpx

from evaluation.run_dev import dev_cases
from evaluation.views import build as build_view
from evidencerca import rca_input
from evidencerca.core import Windows, detect_alert, make_windows
from evidencerca.evidence import View, normalize
from evidencerca.methods.evidencerca_e0 import diagnose_full
from harness.leakage import scan_tokens
from phase4.agent import conditions, evidence_pack, prompts
from phase4.agent.evidence_pack import median_time
from phase4.agent.llm_client import LLMSettings, OllamaClient

OUT = Path("results/phase4a/structured_output_tests.json")
DEV_CASE_PREFIX = "a4f3f9b6"     # dev seed 11, processing_latency:inventory (any dev case would do)
DIGEST = "sha256:" + "0" * 64


def mock_client(responses, settings=LLMSettings(timeout_s=5.0)):
    """responses: list of (status, body | Exception) served in order to /api/chat."""
    queue = list(responses)
    seen = []

    def handler(req: httpx.Request):
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "mock"})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.model, "digest": DIGEST}]})
        seen.append(json.loads(req.content))
        status, body = queue.pop(0)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)
    return OllamaClient(settings, transport=httpx.MockTransport(handler)), seen


def ok_body(content: str, done_reason="stop"):
    return {"message": {"role": "assistant", "content": content}, "done": True, "done_reason": done_reason,
            "prompt_eval_count": 2000, "eval_count": 300, "load_duration": 1e9, "prompt_eval_duration": 1e10,
            "eval_duration": 2e10, "total_duration": 3.1e10}


def answer_from_e0(view, win, condition, mutate=None) -> tuple[str, dict]:
    """A schema-valid answer: E0's claims re-expressed with aliases where the table holds their records (E1), else
    latency claims copied from server spans in the neutral table (B4). Values are exact by construction."""
    e0, _ = diagnose_full(view, win)
    first = e0["evidence_ids"] + [x for e in e0["propagation_path"] for x in e["evidence_ids"]] \
        if condition == "E1" else None
    pk = evidence_pack.build(view, win, first)
    inv = {v: k for k, v in prompts.aliases(pk).items()}
    claims = []
    for c in e0["factual_claims"]:
        if not all(e in inv for e in c["evidence_ids"]):
            continue
        claims.append({"kind": c["kind"], "predicate": c["predicate"], "subject_service": c["subject_service"],
                       "object_service": c["object_service"] or "none", "value": c["value"] or 0,
                       "unit": c["unit"] or "1", "evidence": [inv[e] for e in c["evidence_ids"]]})
    if not claims:   # B4's neutral table need not hold E0's records: one latency claim per cited server span
        for alias, eid in prompts.aliases(pk).items():
            r = view.by_id[eid]
            if r["modality"] == "trace" and r["kind"] == "SERVER" and len(claims) < 3:
                claims.append({"kind": "observed", "predicate": "latency_above_baseline", "subject_service": r["service"],
                               "object_service": "none", "value": r["value"], "unit": "ms", "evidence": [alias]})
    obj = {"decision": "DIAGNOSE", "suspected_service": e0["suspected_service"],
           "suspected_fault_type": e0["suspected_fault_type"],
           "ranked_candidates": [c["service"] for c in e0["ranked_candidates"][:3]], "claims": claims[:6],
           "propagation_path": [], "uncertainty_reasons": [], "explanation": "test"}
    if mutate:
        mutate(obj)
    return json.dumps(obj), obj


def run_scenarios(view, win) -> list[dict]:
    res = []

    def record(name, expect, out, extra=None):
        got = {"call_outcome": (out.get("call") or {}).get("outcome"), "parse": (out.get("parse") or {}).get("status"),
               "attempts": len((out.get("call") or {}).get("attempts", [])), "called": out["called"],
               "semantic_issues": out.get("semantic_issues"), "decision": out.get("decision"),
               "n_pre": len((out.get("claims") or {}).get("pre_filter", [])),
               "n_post": len((out.get("claims") or {}).get("post_filter", []))}
        got.update(extra or {})
        passed = all(got.get(k) == v for k, v in expect.items())
        res.append({"name": name, "expected": expect, "observed": got, "passed": passed})

    # 1 valid answer built from E0's claims: parses, verifies, nothing rewritten
    for cond in ("B4", "E1"):
        text, obj = answer_from_e0(view, win, cond)
        cl, _ = mock_client([(200, ok_body(text))])
        out = conditions.run(cond, view, win, cl)
        n_obs = sum(c["kind"] == "observed" for c in obj["claims"])
        record(f"{cond}: valid answer re-expressing E0 claims", {"call_outcome": "ok", "parse": "valid", "attempts": 1, "all_pre_filter_kept": True,
                                                                       "aliases_mapped_to_view_ids": True},
               out, {"n_observed": n_obs, "all_pre_filter_kept": len(out["claims"]["pre_filter"]) == len(obj["claims"]),
                     "aliases_mapped_to_view_ids": all(e in view.by_id for c in out["claims"]["pre_filter"]
                                                       for e in c["evidence_ids"])})
    # 2 truncated JSON (token limit) -> malformed_json, no retry, ABSTAIN
    text, _ = answer_from_e0(view, win, "B4")
    cl, _ = mock_client([(200, ok_body(text[: len(text) // 2], done_reason="length"))])
    out = conditions.run("B4", view, win, cl)
    record("truncated output", {"parse": "malformed_json", "attempts": 1, "decision": "ABSTAIN", "n_pre": 0, "truncated_flag": True}, out,
           {"truncated_flag": out["call"]["truncated_at_token_limit"]})
    # 3 schema violations
    for name, mut in [("missing key", lambda o: o.pop("explanation")),
                      ("bad enum", lambda o: o.update(suspected_service="database")),
                      ("too many claims", lambda o: o.update(claims=o["claims"] * 4)),
                      ("unexpected key", lambda o: o.update(confidence=0.9)),
                      ("string value", lambda o: o["claims"][0].update(value="812 ms"))]:
        text, _ = answer_from_e0(view, win, "B4", mut)
        cl, _ = mock_client([(200, ok_body(text))])
        out = conditions.run("B4", view, win, cl)
        record(f"schema: {name}", {"parse": "schema_noncompliant", "decision": "ABSTAIN", "n_pre": 0}, out,
               {"errors": out["parse"]["errors"][:3]})
    # 4 invented evidence alias -> kept verbatim, V1 rejects the claim
    text, _ = answer_from_e0(view, win, "B4", lambda o: o["claims"][0].update(evidence=["E99"]))
    cl, _ = mock_client([(200, ok_body(text))])
    out = conditions.run("B4", view, win, cl)
    c1 = next(c for c in out["verification"]["claims"] if c["claim_id"] == "c1")
    record("invented citation", {"parse": "valid", "c1_V1": "fail", "c1_verified": False, "c1_cites_kept_verbatim": True}, out, {"c1_V1": c1["checks"].get("V1"), "c1_verified": c1["verified"],
                                                          "c1_cites_kept_verbatim": out["claims"]["pre_filter"][0]["evidence_ids"] == ["E99"]})
    # 5 wrong value -> V4 fails, claim rejected and preserved unchanged
    text, obj = answer_from_e0(view, win, "B4", lambda o: o["claims"][0].update(value=o["claims"][0]["value"] * 3 + 1))
    cl, _ = mock_client([(200, ok_body(text))])
    out = conditions.run("B4", view, win, cl)
    c1 = next(c for c in out["verification"]["claims"] if c["claim_id"] == "c1")
    record("wrong value", {"parse": "valid", "c1_V4": "fail", "c1_verified": False, "value_unchanged": True,
                            "c1_in_post_filter": False}, out, {"c1_V4": c1["checks"].get("V4"), "c1_verified": c1["verified"],
                                                    "value_unchanged": out["claims"]["pre_filter"][0]["value"] == obj["claims"][0]["value"],
                                                    "c1_in_post_filter": any(c["claim_id"] == "c1" for c in out["claims"]["post_filter"])})
    # 6 timeouts: retried once, then reported as timeout
    req = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    cl, _ = mock_client([(0, httpx.ReadTimeout("t", request=req)), (0, httpx.ReadTimeout("t", request=req))])
    out = conditions.run("B4", view, win, cl)
    record("timeout twice", {"call_outcome": "timeout", "attempts": 2, "parse": "no_response", "decision": "ABSTAIN"}, out)
    # 7 transient 500 then success
    text, _ = answer_from_e0(view, win, "B4")
    cl, _ = mock_client([(500, {"error": "runner crashed"}), (200, ok_body(text))])
    out = conditions.run("B4", view, win, cl)
    record("HTTP 500 then ok", {"call_outcome": "ok", "attempts": 2, "parse": "valid"}, out)
    # 8 client error (unknown model) -> model_failure, no retry
    cl, _ = mock_client([(404, {"error": "model not found"})])
    out = conditions.run("B4", view, win, cl)
    record("HTTP 404", {"call_outcome": "model_failure", "attempts": 1, "parse": "no_response"}, out)
    # 9 inconsistent decision -> semantic issue, B4 decision forced to ABSTAIN only because no service is named
    text, _ = answer_from_e0(view, win, "B4", lambda o: o.update(suspected_service="none"))
    cl, _ = mock_client([(200, ok_body(text))])
    out = conditions.run("B4", view, win, cl)
    record("DIAGNOSE with none", {"parse": "valid", "decision": "ABSTAIN",
                                  "semantic_issues": ["DIAGNOSE with suspected_service none"]}, out)
    # 10 E1 keeps E0's diagnosis-level outcome even if the model names another service
    text, _ = answer_from_e0(view, win, "E1", lambda o: o.update(suspected_service="payment"))
    cl, _ = mock_client([(200, ok_body(text))])
    out = conditions.run("E1", view, win, cl)
    record("E1 model disagrees with E0", {"parse": "valid", "e1_suspected_is_e0": True, "llm_disagreement_recorded": True}, out,
           {"e1_suspected_is_e0": out["diagnosis"]["suspected_service"] == out["e0"]["suspected_service"],
            "llm_disagreement_recorded": out["llm"]["agrees_with_e0_top1"] is False})
    # 11 no alert -> no call at all
    cl, seen = mock_client([])
    out = conditions.run("B4", view, Windows(False, "alert", None, None, None, None, "no SLO deviation detected"), cl)
    record("no alert", {"called": False, "decision": "ABSTAIN", "requests_sent": 0}, out, {"requests_sent": len(seen)})
    # 12 request carries the bounded settings
    text, _ = answer_from_e0(view, win, "B4")
    cl, seen = mock_client([(200, ok_body(text))])
    conditions.run("B4", view, win, cl)
    o = seen[0]["options"]
    res.append({"name": "request settings", "observed": {"options": o, "think": seen[0]["think"],
                                                          "format_is_schema": isinstance(seen[0]["format"], dict)},
                "passed": o["temperature"] == 0.0 and o["num_predict"] == 1024 and seen[0]["think"] is False
                and isinstance(seen[0]["format"], dict)})
    return res


def leakage_scan() -> dict:
    """Case-dependent prompt text over every dev primary case, views C0 and MT, B4 and E1."""
    rows, systems = [], set()
    for case in dev_cases():
        if case["role"] != "primary":
            continue
        clean = normalize(rca_input.load_case(case["case_id"]))
        for spec in ("C0", "MT"):
            view, _ = build_view(clean, case["case_id"], spec, 1, case["gt"])
            win = detect_alert(view)
            if not win.triggered:
                win = make_windows(view, median_time(view), "fixed-median")
            e0, _ = diagnose_full(view, win)
            for cond in ("B4", "E1"):
                ranking = e0 if cond == "E1" and e0["decision"] == "DIAGNOSE" else None
                first = (e0["evidence_ids"] if ranking else None)
                pk = evidence_pack.build(view, win, first)
                text = prompts.case_text_only(pk, ranking)
                # E0's own predicted fault type is an E0 output (frozen, agent side), not a label; its line is
                # excluded from the token scan, everything else is scanned.
                scanned = "\n".join(l for l in text.splitlines() if not l.startswith("EvidenceRCA fault type:"))
                hits = scan_tokens(scanned.encode())
                gt_strings = [case["gt"]["scenario"]["key"], str(case["gt"]["timeline_vm_ns"].get("injection_start_ns"))]
                rows.append({"case": case["case_id"][:8], "view": spec, "condition": cond, "records": len(pk.records),
                             "forbidden_token_hits": hits, "gt_strings_present": [g for g in gt_strings if g in text],
                             "case_id_present": case["case_id"] in text})
                msgs, _ = prompts.build_messages(pk, cond, ranking) if (cond == "B4" or ranking) else (None, None)
                if msgs:
                    systems.add(msgs[0]["content"])
    bad = [r for r in rows if r["forbidden_token_hits"] or r["gt_strings_present"] or r["case_id_present"]]
    equal = all(a["records"] == b["records"] for a, b in zip(rows[0::2], rows[1::2]))
    return {"prompts_scanned": len(rows), "violations": bad, "distinct_system_prompts": len(systems),
            "record_count_equal_B4_E1": equal, "passed": not bad and len(systems) == 1 and equal}


def main() -> int:
    cid = next(c["case_id"] for c in dev_cases() if c["case_id"].startswith(DEV_CASE_PREFIX))
    view = View("structured-test", cid, normalize(rca_input.load_case(cid)))
    win = detect_alert(view)
    scen = run_scenarios(view, win)
    leak = leakage_scan()
    rep = {"schema": "evidencerca.phase4a.structured_output_tests/v1", "prompt_version": prompts.PROMPT_VERSION,
           "split": "dev seed 11 only", "model_calls": 0, "scenarios": scen, "prompt_leakage": leak,
           "passed": all(s["passed"] for s in scen) and leak["passed"]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    for s in scen:
        print("PASS" if s["passed"] else "FAIL", s["name"], "" if s["passed"] else s["observed"])
    print("prompt leakage:", "PASS" if leak["passed"] else "FAIL", leak["prompts_scanned"], "prompts")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
