"""Phase 4A-v3 development evaluation: E1 vs E2 (deterministic ownership) and B4 vs B4-G (detector gate).

  uv run python -m phase4.eval.pilot_v3 plan       # reuse map + re-execution subset -> results/phase4a_v3/plan.json
  uv run python -m phase4.eval.pilot_v3 reverify   # NEW model calls: re-executes the predeclared subset live
  uv run python -m phase4.eval.pilot_v3 analyze    # dev_comparison / ownership_analysis / control_gate_results

Pre-registered rules (fixed here and committed before any new call; dev seed 11 only):
  views     the same 20 views as the Phase 4A-v2 pilot (results/phase4a_v2/pilot_plan.json), no reselection.
  reuse     E2 post-processes E1's p4a-v1 output; B4-G calls the unchanged p4a-v1 B4 on the detector's window,
            which is the window the pilot used whenever the detector alerted. Both therefore reuse the pilot's
            p4a-v1 E1/B4 calls; every reused call is listed with its request, prompt, evidence and output hashes.
            Reused calls are reported as reused, never as new calls.
  reverify  new live calls through the E2 and B4-G code paths for: per category, the lowest-index view whose E1
            trial made a call (5 E2 calls), and the lowest-index view whose detector alerts (4 B4-G calls; the
            controls do not alert). Reuse is accepted only if every re-executed request hash and raw text equals
            the pilot's; otherwise all reused trials would be re-run live (and reported as such).
  windows   E1/E2: the pilot window rule (alert window, else median-time window) so E1 and E2 are matched; a
            secondary "gated" reading applies the frozen protocol (no alert -> E0 ABSTAIN). B4-G: detector only.
  coverage  the detector's alert coverage on every dev-seed-11 primary case x all 18 views (no LLM).
Ground truth is read only by this evaluation module, after outputs exist.
"""

import hashlib
import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from evaluation.freeze import verify_frozen
from evaluation.run_dev import dev_cases
from evaluation.views import SPECS, build as build_view
from evidencerca import rca_input
from evidencerca.core import detect_alert
from evidencerca.evidence import normalize
from evidencerca.verifier import verify
from phase4.agent import conditions_e2, ownership, prompts
from phase4.agent.llm_client import LLMSettings, OllamaClient
from phase4.eval import pilot_v2
from phase4.eval.benchmark import Sampler, environment, system_memory

OUT = Path("results/phase4a_v3")
V2 = Path("results/phase4a_v2")
PLAN = OUT / "plan.json"
AUDIT_SEED = 20261016
sha = lambda s: hashlib.sha256((s or "").encode()).hexdigest()


def _pilot_records():
    return {(r["view"]["index"], r["condition"], r["version"]): r
            for r in (json.loads(p.read_text()) for p in sorted((V2 / "raw").glob("*.json")))}


def make_plan() -> dict:
    v2plan = json.loads((V2 / "pilot_plan.json").read_text())
    recs = _pilot_records()
    rows = []
    for v in v2plan["views"]:
        ent = {k: v[k] for k in ("index", "category", "case_id", "scenario_key", "spec", "perturbation_seed")}
        for cond, new in (("E1", "E2"), ("B4", "B4-G")):
            r = recs[(v["index"], cond, "p4a-v1")]
            c = r["call_record"]
            reused = r["output"].get("called") and (cond == "E1" or r["alert_fired"])
            ent[new] = {"source": f"results/phase4a_v2/raw/{r['k']:03d}_v{v['index']:02d}_{cond}_p4a-v1.json",
                        "reuses_call": bool(reused), "alert_fired": r["alert_fired"],
                        "request_sha256": c["request_sha256"] if c else None,
                        "system_sha256": (c or {}).get("prompt_meta", {}).get("system_sha256"),
                        "case_text_sha256": (c or {}).get("prompt_meta", {}).get("case_text_sha256"),
                        "raw_text_sha256": sha(c["raw_text"]) if c else None,
                        "model_digest": (c or {}).get("model_digest")}
        rows.append(ent)
    sub = {"E2": [], "B4-G": []}
    for cat in ("straightforward", "missing_trace", "conflicting", "control", "cascade"):
        e2 = [r["index"] for r in rows if r["category"] == cat and r["E2"]["reuses_call"]]
        bg = [r["index"] for r in rows if r["category"] == cat and r["B4-G"]["alert_fired"]]
        sub["E2"] += e2[:1]
        sub["B4-G"] += bg[:1]
    return {"schema": "evidencerca.phase4a_v3.plan/v1", "split": "dev seed 11 only",
            "rules": __doc__.split("Pre-registered rules")[1].split("Ground truth")[0].strip(),
            "prompt_version": prompts.PROMPT_VERSION, "transform_version": ownership.TRANSFORM_VERSION,
            "views": rows, "reverify": sub, "n_new_calls_planned": len(sub["E2"]) + len(sub["B4-G"])}


def _load(row):
    return pilot_v2.load_view(row)


# ---------------------------------------------------------------- live re-execution (new calls)
def reverify() -> int:
    verify_frozen()
    plan = json.loads(PLAN.read_text())
    assert plan == json.loads(json.dumps(make_plan())), "plan.json differs from the coded rule"
    env0 = environment()
    if env0["colima_running"] or "AC Power" not in (env0["power"] or ""):
        print(f"precondition failed: colima={env0['colima_running']} power={env0['power']}", file=sys.stderr)
        return 2
    client = OllamaClient(LLMSettings())
    if client.digest is None:
        print("qwen3:4b not available", file=sys.stderr)
        return 2
    rows = {r["index"]: r for r in plan["views"]}
    d = OUT / "raw_reverify"
    d.mkdir(parents=True, exist_ok=True)
    res = []
    for cond in ("E2", "B4-G"):
        for vi in plan["reverify"][cond]:
            row = rows[vi]
            view, win, fired = _load(row)
            s = Sampler()
            s.start()
            t0 = time.perf_counter()
            out = conditions_e2.run_e2(view, win, client) if cond == "E2" else conditions_e2.run_b4g(view, client)
            wall = time.perf_counter() - t0
            peak = s.stop()
            call = out.pop("call", None) or {}
            ref = row[cond]
            ent = {"condition": cond, "view_index": vi, "category": row["category"], "spec": row["spec"],
                   "new_call": bool(call), "outcome": call.get("outcome"), "wall_s": round(wall, 2),
                   "prompt_tokens": call.get("prompt_tokens"), "output_tokens": call.get("output_tokens"),
                   "ollama_rss_peak_mb": peak, "request_sha256": call.get("request_sha256"),
                   "same_request_as_pilot": call.get("request_sha256") == ref["request_sha256"],
                   "identical_raw_text": sha(call.get("raw_text")) == ref["raw_text_sha256"],
                   "model_digest": call.get("model_digest")}
            res.append(ent)
            (d / f"{cond}_v{vi:02d}.json").write_text(json.dumps({"entry": ent, "call_record": call, "output": out},
                                                                 indent=1, default=str) + "\n")
            print(ent, flush=True)
    client.unload()
    rep = {"schema": "evidencerca.phase4a_v3.reuse_verification/v1",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "environment_before": env0,
           "memory_after": system_memory(), "calls": res, "new_llm_calls": sum(r["new_call"] for r in res),
           "reuse_accepted": all(r["same_request_as_pilot"] and r["identical_raw_text"] for r in res)}
    (OUT / "reuse_verification.json").write_text(json.dumps(rep, indent=1, default=str) + "\n")
    print("reuse accepted:", rep["reuse_accepted"])
    return 0 if rep["reuse_accepted"] else 1


# ---------------------------------------------------------------- analysis (evaluation side)
def _gt(cid, cache={}):
    if not cache:
        cache.update({c["case_id"]: c["gt"] for c in dev_cases()})
    return cache[cid]


def detector_coverage() -> dict:
    """Frozen detector over every dev-seed-11 primary case x 18 views (no LLM)."""
    rows = []
    for c in dev_cases():
        if c["role"] != "primary":
            continue
        clean = normalize(rca_input.load_case(c["case_id"]))
        for spec, seed in SPECS:
            view, _ = build_view(clean, c["case_id"], spec, seed, c["gt"])
            rows.append({"scenario_key": c["gt"]["scenario"]["key"], "spec": f"{spec}:{seed}",
                         "fault": bool(c["gt"]["primary_root_services"]), "alert": detect_alert(view).triggered})
    f = [r for r in rows if r["fault"]]
    h = [r for r in rows if not r["fault"]]
    return {"views": len(rows), "base_cases": len({r["scenario_key"] for r in rows}),
            "fault_views": len(f), "fault_views_alerted": sum(r["alert"] for r in f),
            "fault_coverage": round(sum(r["alert"] for r in f) / len(f), 4),
            "missed_fault_views": sorted(f"{r['scenario_key']} {r['spec']}" for r in f if not r["alert"]),
            "healthy_views": len(h), "healthy_false_alerts": sum(r["alert"] for r in h),
            "healthy_false_alert_views": sorted(f"{r['scenario_key']} {r['spec']}" for r in h if r["alert"])}


def _obs(ver):
    return [c for c in (ver or {}).get("claims", []) if c["kind"] == "observed"]


def analyze() -> int:
    plan = json.loads(PLAN.read_text())
    recs = _pilot_records()
    trials, own_rows, gate_rows = [], [], []
    current = None
    for row in plan["views"]:
        gt = _gt(row["case_id"])
        roots = gt["primary_root_services"]
        e1 = recs[(row["index"], "E1", "p4a-v1")]
        b4 = recs[(row["index"], "B4", "p4a-v1")]
        view = None
        if e1["output"].get("called"):
            view, _, _ = _load(row)
            # the stored E1 verification must be exactly what the frozen verifier gives now
            assert verify(e1["output"]["diagnosis"], view) == json.loads(json.dumps(e1["output"]["verification"])), row
        e2 = conditions_e2.e2_from_e1(e1["output"], view) if view is not None else \
            dict(e1["output"], condition="E2", ownership=None)
        # B4-G: gate decision is the frozen detector's (recomputed on the view); the call is the pilot's B4 call
        if view is None:
            view, _, _ = _load(row)
        gate = detect_alert(view).triggered
        assert gate == b4["alert_fired"], row
        b4g = b4["output"] if gate else {"condition": "B4-G", "called": False, "decision": "ABSTAIN",
                                         "note": conditions_e2.GATE_NO_CALL}
        for cond, out, src in (("E1", e1["output"], e1), ("E2", e2, e1), ("B4", b4["output"], b4), ("B4-G", b4g, b4)):
            called = bool(out.get("called"))
            call = src["call_record"] if called else None
            llm = out.get("llm") or {}
            ver = out.get("verification") if called else None
            obs = _obs(ver)
            t = {"view_index": row["index"], "category": row["category"], "scenario_key": row["scenario_key"],
                 "spec": row["spec"], "condition": cond, "fault_present": bool(roots), "alert": gate,
                 "called": called, "reused_call": called, "decision": out.get("decision"),
                 "suspect": (out.get("diagnosis") or {}).get("suspected_service") if called else
                 ((out.get("e0") or {}).get("suspected_service") if cond in ("E1", "E2") else None),
                 "llm_suspect": llm.get("suspected_service"), "llm_fault_type": llm.get("suspected_fault_type"),
                 "top1": None, "fault_type_correct": None,
                 "observed": len(obs), "observed_verified": sum(c["verified"] for c in obs),
                 "v3_fail": sum(c["checks"].get("V3") == "fail" for c in obs),
                 "wall_s": (call or {}).get("wall_s"), "prompt_tokens": (call or {}).get("prompt_tokens"),
                 "output_tokens": (call or {}).get("output_tokens"), "ollama_rss_peak_mb": src["ollama_rss_peak_mb"] if called else None}
            if cond in ("E1", "E2"):   # secondary gated reading: frozen protocol abstains without an alert
                t["decision_gated"] = t["decision"] if gate else "ABSTAIN"
            if roots:
                diag = out.get("diagnosis") or {}
                t["top1"] = t["decision"] == "DIAGNOSE" and t["suspect"] in roots
                ft = diag.get("suspected_fault_type") if cond in ("E1", "E2") else llm.get("suspected_fault_type")
                t["fault_type_correct"] = ft == gt["fault_type"]
            if cond == "E2" and out.get("ownership"):
                cv = _obs(out.get("canonical_verification"))
                disp = [a["disposition"] for a in out["ownership"]["assessments"]]
                t.update(canonical=len(cv), canonical_verified=sum(c["verified"] for c in cv),
                         dispositions={d: disp.count(d) for d in ("consistent", "transformed", "rejected", "not_applicable")},
                         verified_original_plus_canonical=t["observed_verified"] + sum(c["verified"] for c in cv))
                vmap = {c["claim_id"]: c for c in (out.get("verification") or {}).get("claims", [])}
                cmap = {c["claim_id"]: c for c in (out.get("canonical_verification") or {}).get("claims", [])}
                for a in out["ownership"]["assessments"]:
                    o = vmap.get(a["claim_id"], {})
                    cc = cmap.get(f"{a['claim_id']}~canon")
                    own_rows.append({"view_index": row["index"], "category": row["category"],
                                     "scenario_key": row["scenario_key"], "spec": row["spec"], **{
                                         k: a.get(k) for k in ("claim_id", "predicate", "kind", "disposition", "reason",
                                                               "original_subject", "implied_subject")},
                                     "subject_is_root": a["original_subject"] in roots,
                                     "implied_is_root": a.get("implied_subject") in roots if a.get("implied_subject") else None,
                                     "changes": (a.get("audit") or {}).get("changes"),
                                     "original_verified": bool(o.get("verified")), "original_checks": o.get("checks"),
                                     "canonical_verified": bool(cc["verified"]) if cc else None,
                                     "canonical_checks": cc["checks"] if cc else None,
                                     "cited_provenance": a["cited_provenance"], "audit": a.get("audit"),
                                     "original_claim": next(c for c in out["claims"]["original_pre_filter"]
                                                            if c["claim_id"] == a["claim_id"]),
                                     "canonical_claim": a.get("canonical")})
            trials.append(t)
        gate_rows.append({"view_index": row["index"], "category": row["category"], "scenario_key": row["scenario_key"],
                          "spec": row["spec"], "fault_present": bool(roots), "alert": gate,
                          "b4_decision": b4["output"].get("decision"), "b4_called": bool(b4["output"].get("called")),
                          "b4g_decision": b4g["decision"], "b4g_called": bool(b4g.get("called")),
                          "b4_parse": (b4["output"].get("parse") or {}).get("status")})
    groups = {c: _summ([t for t in trials if t["condition"] == c]) for c in ("E1", "E2", "B4", "B4-G")}
    by_cat = {c: {cat: _summ([t for t in trials if t["condition"] == c and t["category"] == cat])
                  for cat in ("control", "straightforward", "cascade", "missing_trace", "conflicting")}
              for c in groups}
    by_base = {}
    for t in trials:
        b = by_base.setdefault(t["scenario_key"], {"views": set(), "fault_present": t["fault_present"]})
        b["views"].add(t["spec"])
    by_base_case = {k: {"views": sorted(v["views"]), "n_views": len(v["views"]), "fault_present": v["fault_present"],
                        **{c: _summ([t for t in trials if t["condition"] == c and t["scenario_key"] == k])
                           for c in ("E1", "E2", "B4", "B4-G")}} for k, v in by_base.items()}
    reuse = json.loads((OUT / "reuse_verification.json").read_text()) if (OUT / "reuse_verification.json").exists() else None
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "split": "dev seed 11 only",
            "views": len(plan["views"]), "base_cases": len(by_base),
            "llm_calls_reused_from_phase4a_v2": sum(t["reused_call"] for t in trials if t["condition"] in ("E2", "B4-G")),
            "new_llm_calls_this_phase": (reuse or {}).get("new_llm_calls", 0),
            "reuse_verification": {k: (reuse or {}).get(k) for k in ("reuse_accepted", "new_llm_calls")},
            "prompt_version": "p4a-v1 (primary reference); p4a-v2 not used", "transform_version": ownership.TRANSFORM_VERSION}
    cov = detector_coverage()
    OUT.joinpath("dev_comparison.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4a_v3.dev_comparison/v1", **meta, "groups": groups, "by_category": by_cat,
         "by_base_case": by_base_case, "trials": trials,
         "cost": {"api_cost_usd": 0.0, "note": "local inference only"}}, indent=1, default=str) + "\n")
    disp = {d: sum(r["disposition"] == d for r in own_rows) for d in ("consistent", "transformed", "rejected", "not_applicable")}
    obs_rows = [r for r in own_rows if r["kind"] == "observed"]
    tr = [r for r in own_rows if r["disposition"] == "transformed"]
    rej = [r for r in own_rows if r["disposition"] == "rejected"]
    summary = {
        "claims_total": len(own_rows), "observed": len(obs_rows), "dispositions": disp,
        "transformation_rate_observed": round(len(tr) / len(obs_rows), 4) if obs_rows else None,
        "rejection_rate_observed": round(len(rej) / len(obs_rows), 4) if obs_rows else None,
        "subject_ownership_errors": sum(1 for r in tr if "subject_service" in (r["changes"] or {})),
        "unit_changes": sum(1 for r in tr if "unit" in (r["changes"] or {})),
        "object_service_cleared": sum(1 for r in tr if "object_service" in (r["changes"] or {})),
        "rejected_reasons": _count(r["reason"].split(" [")[0].split(" {")[0] for r in rej),
        "original_verified": sum(r["original_verified"] for r in obs_rows),
        "original_verified_rate": round(sum(r["original_verified"] for r in obs_rows) / len(obs_rows), 4) if obs_rows else None,
        "canonical_candidates": len(tr), "canonical_verified": sum(bool(r["canonical_verified"]) for r in tr),
        "canonical_verified_rate": round(sum(bool(r["canonical_verified"]) for r in tr) / len(tr), 4) if tr else None,
        "canonical_first_failed_check": _count(next((k for k, v in (r["canonical_checks"] or {}).items() if v == "fail"), None)
                                               for r in tr if not r["canonical_verified"]),
        "transformed_whose_original_was_verified": sum(r["original_verified"] for r in tr),
        "canonical_subject_is_root": sum(bool(r["implied_is_root"]) for r in tr),
        "canonical_verified_subject_is_root": sum(bool(r["implied_is_root"]) and bool(r["canonical_verified"]) for r in tr),
        "disposition_vs_original_V3": {d: _count((r["original_checks"] or {}).get("V3") for r in obs_rows if r["disposition"] == d)
                                       for d in ("consistent", "transformed", "rejected")},
        "consistent_but_not_verified_first_check": _count(next((k for k, v in (r["original_checks"] or {}).items() if v == "fail"), None)
                                                          for r in obs_rows if r["disposition"] == "consistent" and not r["original_verified"]),
    }
    OUT.joinpath("ownership_analysis.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4a_v3.ownership_analysis/v1", **meta,
         "note": "verifier verdicts are the frozen verifier's (unchanged), not human ground truth; ownership "
                 "dispositions and V3 share their association rules by construction, so their agreement is not "
                 "independent evidence", "summary": summary, "claims": own_rows}, indent=1, default=str) + "\n")
    g = lambda rows, k: sum(bool(r[k]) for r in rows)
    fault = [r for r in gate_rows if r["fault_present"]]
    ctrl = [r for r in gate_rows if not r["fault_present"]]
    OUT.joinpath("control_gate_results.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4a_v3.control_gate_results/v1", **meta,
         "definitions": {"B4-G": "frozen detector gate + unchanged p4a-v1 B4; not an LLM-only baseline",
                         "false_diagnosis": "DIAGNOSE on a healthy control view",
                         "missed_fault": "fault view on which the gate did not alert (B4-G abstains without a call)"},
         "pilot_20_views": {
             "fault_views": len(fault), "control_views": len(ctrl),
             "gate_alerts_on_fault_views": g(fault, "alert"), "gate_alerts_on_controls": g(ctrl, "alert"),
             "missed_faults": len(fault) - g(fault, "alert"),
             "B4_false_diagnoses_on_controls": sum(r["b4_decision"] == "DIAGNOSE" for r in ctrl),
             "B4_controls_malformed": sum(r["b4_parse"] == "malformed_json" for r in ctrl),
             "B4G_false_diagnoses_on_controls": sum(r["b4g_decision"] == "DIAGNOSE" for r in ctrl),
             "B4_calls": g(gate_rows, "b4_called"), "B4G_calls": g(gate_rows, "b4g_called"),
             "calls_avoided": g(gate_rows, "b4_called") - g(gate_rows, "b4g_called"),
             "B4G_false_diagnoses_on_fault_views_wrong_service": sum(
                 1 for t in trials if t["condition"] == "B4-G" and t["fault_present"] and t["decision"] == "DIAGNOSE"
                 and not t["top1"])},
         "detector_coverage_all_dev_views": cov, "rows": gate_rows}, indent=1, default=str) + "\n")
    audit_dry_run(own_rows)
    print(json.dumps({k: {x: groups[k][x] for x in ("called", "decisions", "top1", "control_false_diagnoses",
                                                    "observed_verified", "observed", "verified_per_call")}
                      for k in groups}, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))
    print("coverage:", {k: v for k, v in cov.items() if not isinstance(v, list)})
    return 0


def _count(xs):
    out = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out


def _summ(ts):
    called = [t for t in ts if t["called"]]
    fault = [t for t in ts if t["fault_present"]]
    ctrl = [t for t in ts if not t["fault_present"]]
    m = lambda k: round(statistics.mean([t[k] for t in called if t[k] is not None]), 2) if called else None
    s = {"trials": len(ts), "called": len(called), "base_cases": len({t["scenario_key"] for t in ts}),
         "decisions": {d: sum(t["decision"] == d for t in ts) for d in ("DIAGNOSE", "ABSTAIN")},
         "fault_views": len(fault), "control_views": len(ctrl),
         "top1": sum(bool(t["top1"]) for t in fault), "fault_abstained": sum(t["decision"] == "ABSTAIN" for t in fault),
         "fault_type_correct": sum(bool(t["fault_type_correct"]) for t in fault),
         "control_false_diagnoses": sum(t["decision"] == "DIAGNOSE" for t in ctrl),
         "observed": sum(t["observed"] for t in called), "observed_verified": sum(t["observed_verified"] for t in called),
         "v3_fail": sum(t["v3_fail"] for t in called),
         "verified_per_call": round(sum(t["observed_verified"] for t in called) / len(called), 3) if called else None,
         "wall_s_mean": m("wall_s"), "prompt_tokens_mean": m("prompt_tokens"), "output_tokens_mean": m("output_tokens"),
         "ollama_rss_peak_mb": max((t["ollama_rss_peak_mb"] or 0 for t in called), default=None)}
    if ts and "decision_gated" in ts[0]:
        s["gated_reading"] = {"top1": sum(t["decision_gated"] == "DIAGNOSE" and bool(t["top1"]) for t in fault),
                              "control_false_diagnoses": sum(t["decision_gated"] == "DIAGNOSE" for t in ctrl)}
    if ts and "canonical" in next((t for t in ts if t["called"]), {}):
        s["canonical"] = sum(t.get("canonical", 0) for t in called)
        s["canonical_verified"] = sum(t.get("canonical_verified", 0) for t in called)
        s["verified_original_plus_canonical_per_call"] = round(
            sum(t.get("verified_original_plus_canonical", 0) for t in called) / len(called), 3) if called else None
    return s


# ---------------------------------------------------------------- audit dry run (origin distinguishability)
def _render(c):
    return (f"{c['kind'].upper()}: {c['predicate']}(subject={c['subject_service']}"
            + (f", object={c['object_service']}" if c.get("object_service") else "") + ")"
            + (f" = {c['value']} {c['unit']}" if c["kind"] == "observed" else ""))


def _rec_line(p):
    if p.get("missing"):
        return "NOT IN VIEW"
    who = f"{p['emitter']} -> {p['callee']}" if p["family"] == "call" else p["emitter"]
    return f"{who} | {p['modality']} {p['span_kind'] or ''} {p['operation'] or p['measurement']} | unit {p['unit']}".replace("  ", " ")


def audit_dry_run(own_rows, n_pairs=20, n_consistent=20) -> dict:
    """Two blinded sheets (A, B). Each transformed claim's original and canonical version go to DIFFERENT sheets
    (random side), so no sheet holds both members of a pair; consistent original claims are split evenly as
    distractors. Annotators label support (supported / unsupported / partially_supported / not_assessable) and
    guess the origin (model-written / system-derived / cannot tell). The key is sealed."""
    import csv
    rng = random.Random(AUDIT_SEED)
    tr = sorted((r for r in own_rows if r["disposition"] == "transformed"), key=lambda r: (r["view_index"], r["claim_id"]))
    co = sorted((r for r in own_rows if r["disposition"] == "consistent"), key=lambda r: (r["view_index"], r["claim_id"]))
    rng.shuffle(tr)
    rng.shuffle(co)
    tr, co = tr[:n_pairs], co[:n_consistent]
    sheets, key = {"A": [], "B": []}, []
    for i, r in enumerate(tr):
        orig_side = "A" if rng.random() < 0.5 else "B"
        for side, which, claim in ((orig_side, "original", r["original_claim"]),
                                   ("B" if orig_side == "A" else "A", "canonical", r["canonical_claim"])):
            sheets[side].append((which, r, claim))
    for i, r in enumerate(co):
        sheets["A" if i % 2 == 0 else "B"].append(("original_consistent", r, r["original_claim"]))
    d = OUT / "audit_dry_run"
    d.mkdir(parents=True, exist_ok=True)
    fields = ["item_id", "context_id", "claim", "cited_records_metadata", "label", "origin_guess", "notes"]
    for side, items in sheets.items():
        rng.shuffle(items)
        rows = []
        for j, (which, r, claim) in enumerate(items):
            iid = f"{side}{j + 1:02d}"
            rows.append({"item_id": iid, "context_id": sha(f"{AUDIT_SEED}|{r['view_index']}")[:10], "claim": _render(claim),
                         "cited_records_metadata": "\n".join(_rec_line(p) for p in r["cited_provenance"]),
                         "label": "", "origin_guess": "", "notes": ""})
            key.append({"item_id": iid, "origin": which, "view_index": r["view_index"], "claim_id": r["claim_id"],
                        "original_verified": r["original_verified"], "canonical_verified": r["canonical_verified"]})
        with (d / f"sheet_{side}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    (d / "key.SEALED.json").write_text(json.dumps({"warning": "do not open before annotation is complete",
                                                    "seed": AUDIT_SEED, "rows": key}, indent=1) + "\n")
    man = {"purpose": "dev dry run: can blinded annotators tell model-written claims from provenance-derived "
                      "canonical transformations? Not the Phase 4B audit; protocol section 9 is unchanged.",
           "pairs": len(tr), "consistent_distractors": len(co), "items_per_sheet": {k: len(v) for k, v in sheets.items()},
           "design": "each pair split across sheets A/B (never both members on one sheet); one annotator per sheet or "
                     "both sheets with a washout; analysis: origin-guess accuracy vs chance, and support labels for "
                     "original vs canonical within pairs (McNemar), after unsealing",
           "labels": ["supported", "unsupported", "partially_supported", "not_assessable"],
           "origin_guess": ["model-written", "system-derived", "cannot tell"]}
    (d / "manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    return man


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan":
        OUT.mkdir(parents=True, exist_ok=True)
        p = make_plan()
        PLAN.write_text(json.dumps(p, indent=1) + "\n")
        print(json.dumps(p["reverify"]), p["n_new_calls_planned"], "new calls planned;",
              sum(v["E2"]["reuses_call"] for v in p["views"]), "E2 reuses,",
              sum(v["B4-G"]["reuses_call"] for v in p["views"]), "B4-G reuses")
        return 0
    if cmd == "reverify":
        return reverify()
    if cmd == "analyze":
        return analyze()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
