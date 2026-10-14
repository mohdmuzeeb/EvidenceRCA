"""Phase 4A-v2 development pilot: p4a-v1 vs p4a-v2, B4 and E1, matched views, dev seed 11 only.

  uv run python -m phase4.eval.pilot_v2 plan      # selection rule -> results/phase4a_v2/pilot_plan.json (no model)
  uv run python -m phase4.eval.pilot_v2 run       # model calls (resumable) -> results/phase4a_v2/raw/
  uv run python -m phase4.eval.pilot_v2 analyze   # -> dev_comparison / claim_analysis / control_abstention + audit

Predeclared case-selection rule (fixed in code and committed before any pilot call; uses scenario metadata only,
never any method's performance; no calibration or test case is opened):
  pool             dev seed 11 primary cases (evaluation.run_dev.dev_cases)
  straightforward  single-service, non-"strong" cases, fault type not cascading/none, ordered by
                   sha256("p4a-v2-pilot|" + scenario key); walk that order and keep the first case of each new
                   fault type until 4 types are covered. View C0.
  missing_trace    the same 4 cases, view MT (traces removed)
  conflicting      the same 4 cases, view CF (a decoy service, not a root and not adjacent to one, whose CPU and
                   latency metrics are shifted in the analysis window)
  control          both healthy cases (no root cause) x views C0 and R40 seed 1
  cascade          the only dev cascading case x views C0, R40 seed 1, MT, CF
  = 20 views; each view runs B4 and E1 under p4a-v1 and p4a-v2 (<= 80 calls). Calls are ordered view by view,
  B4 then E1, with the version order alternating per view (even index v1 first, odd index v2 first).
Window rule (label-free, identical for every view and both versions): the shared detector's alert window if it
alerts, else a window anchored at the median record timestamp (phase4.agent.evidence_pack.median_time).
Model settings: phase4.agent.llm_client.LLMSettings() for both versions. Ground truth is read only to build
evaluation views and, after the calls, to score them.
"""

import csv
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
from evaluation.views import build as build_view
from evidencerca import rca_input
from evidencerca.core import detect_alert, make_windows
from evidencerca.evidence import normalize
from phase4.agent import conditions, conditions_v2, output_schema_v2, prompts, prompts_v2
from phase4.agent.evidence_pack import median_time
from phase4.agent.llm_client import LLMSettings, OllamaClient
from phase4.eval.benchmark import Sampler, environment, system_memory

OUT = Path("results/phase4a_v2")
RAW = OUT / "raw"
PLAN = OUT / "pilot_plan.json"
SALT = "p4a-v2-pilot|"
N_STRAIGHT = 4
AUDIT_SEED, AUDIT_TARGET = 20261015, 200
VERSIONS = {"p4a-v1": conditions, "p4a-v2": conditions_v2}


# ---------------------------------------------------------------- plan
def make_plan() -> dict:
    cases = [c for c in dev_cases() if c["role"] == "primary"]
    assert all(c["gt"]["scenario"]["master_seed"] == 11 for c in cases)
    key = lambda c: c["gt"]["scenario"]["key"]
    single = sorted((c for c in cases if c["gt"]["fault_type"] not in ("none", "cascading_failure")
                     and not key(c).endswith(":strong") and len(c["gt"]["primary_root_services"]) == 1),
                    key=lambda c: hashlib.sha256((SALT + key(c)).encode()).hexdigest())
    straight, types = [], set()
    for c in single:
        if c["gt"]["fault_type"] not in types:
            straight.append(c)
            types.add(c["gt"]["fault_type"])
        if len(straight) == N_STRAIGHT:
            break
    healthy = sorted((c for c in cases if c["gt"]["fault_type"] == "none"), key=key)
    cascade = sorted((c for c in cases if c["gt"]["fault_type"] == "cascading_failure"), key=key)
    views = ([("straightforward", c, "C0", 1) for c in straight] + [("missing_trace", c, "MT", 1) for c in straight]
             + [("conflicting", c, "CF", 1) for c in straight]
             + [("control", c, spec, 1) for c in healthy for spec in ("C0", "R40")]
             + [("cascade", c, spec, 1) for c in cascade for spec in ("C0", "R40", "MT", "CF")])
    rows = []
    for i, (cat, c, spec, seed) in enumerate(views):
        order = ["p4a-v1", "p4a-v2"] if i % 2 == 0 else ["p4a-v2", "p4a-v1"]
        rows.append({"index": i, "category": cat, "case_id": c["case_id"], "scenario_key": key(c), "spec": spec,
                     "perturbation_seed": seed, "version_order": order})
    calls = [{"view_index": r["index"], "condition": cond, "version": v}
             for r in rows for cond in ("B4", "E1") for v in r["version_order"]]
    return {"schema": "evidencerca.phase4a_v2.pilot_plan/v1", "split": "dev seed 11 only",
            "selection_rule": __doc__.split("Predeclared case-selection rule")[1].split("Model settings")[0].strip(),
            "salt": SALT, "settings": LLMSettings().__dict__, "views": rows, "calls": calls,
            "n_views": len(rows), "n_calls_max": len(calls),
            "prompt_system_sha256": {"p4a-v1": hashlib.sha256(prompts.SYSTEM.encode()).hexdigest(),
                                     "p4a-v2": hashlib.sha256(prompts_v2.SYSTEM.encode()).hexdigest()},
            "schema_sha256": {"p4a-v2": hashlib.sha256(json.dumps(output_schema_v2.SCHEMA, sort_keys=True).encode()).hexdigest()}}


# ---------------------------------------------------------------- run
def load_view(row, cache={}):
    cid = row["case_id"]
    if cid not in cache:
        cache.clear()
        cache[cid] = normalize(rca_input.load_case(cid))
    gt = next(c["gt"] for c in dev_cases() if c["case_id"] == cid)
    view, _ = build_view(cache[cid], cid, row["spec"], row["perturbation_seed"], gt)
    win = detect_alert(view)
    fired = win.triggered
    if not fired:
        win = make_windows(view, median_time(view), "fixed-median", reason="label-free: median record timestamp")
    return view, win, fired


def run() -> int:
    verify_frozen()
    plan = json.loads(PLAN.read_text())
    assert plan == json.loads(json.dumps(make_plan())), "pilot_plan.json differs from the coded rule"
    env0 = environment()
    if env0["colima_running"]:
        print("Colima is running: stop it first", file=sys.stderr)
        return 2
    if "AC Power" not in (env0["power"] or ""):
        print(f"not on AC power: {env0['power']}", file=sys.stderr)
        return 2
    client = OllamaClient(LLMSettings())
    if client.digest is None:
        print("qwen3:4b not available on the local Ollama server", file=sys.stderr)
        return 2
    RAW.mkdir(parents=True, exist_ok=True)
    (OUT / "run_logs").mkdir(exist_ok=True)
    (OUT / "run_logs" / f"environment_before_{int(time.time())}.json").write_text(
        json.dumps({"environment": env0, "memory": system_memory(), "model_digest": client.digest,
                    "ollama_version": client.version}, indent=1) + "\n")
    views = {r["index"]: r for r in plan["views"]}
    current, t_all = None, time.time()
    for k, spec in enumerate(plan["calls"]):
        row = views[spec["view_index"]]
        path = RAW / f"{k:03d}_v{row['index']:02d}_{spec['condition']}_{spec['version']}.json"
        if path.exists():
            continue
        if current is None or current[0] != row["index"]:
            view, win, fired = load_view(row)
            current = (row["index"], view, win, fired)
        _, view, win, fired = current
        sampler = Sampler()
        sampler.start()
        t0 = time.perf_counter()
        out = VERSIONS[spec["version"]].run(spec["condition"], view, win, client)
        wall = time.perf_counter() - t0
        peak = sampler.stop()
        call = out.pop("call", None)
        rec = {"k": k, **spec, "view": row, "view_id": view.view_id, "alert_fired": fired, "window_anchor": win.anchor,
               "end_to_end_wall_s": round(wall, 2), "ollama_rss_peak_mb": peak, "system_after": system_memory(),
               "call_record": call, "output": out}
        if out.get("called"):   # the agent-visible prompt, kept for the audit sheet
            pk_msgs = _messages_for(spec, view, win, out)
            rec["user_message"] = pk_msgs[1]["content"]
        path.write_text(json.dumps(rec, indent=1, default=str) + "\n")
        print(f"[{k + 1}/{len(plan['calls'])}] {row['category']:15s} {row['scenario_key']:38s} {row['spec']:3s} "
              f"{spec['condition']} {spec['version']}: called={out.get('called')} "
              f"outcome={(call or {}).get('outcome')} parse={(out.get('parse') or {}).get('status')} "
              f"wall={wall:.1f}s tok={(call or {}).get('prompt_tokens')}/{(call or {}).get('output_tokens')} "
              f"decision={out.get('decision')} llm={(out.get('llm') or {}).get('decision')}", flush=True)
    client.unload()
    print(f"pilot done in {time.time() - t_all:.0f}s")
    return 0


def _messages_for(spec, view, win, out):
    from evidencerca.methods.evidencerca_e0 import diagnose_full
    from phase4.agent import evidence_pack
    ranking, first = None, None
    if spec["condition"] == "E1":
        ranking, _ = diagnose_full(view, win, use_graph=True)
        first = ranking["evidence_ids"] + [x for e in ranking["propagation_path"] for x in e["evidence_ids"]]
    pk = evidence_pack.build(view, win, first)
    mod = prompts if spec["version"] == "p4a-v1" else prompts_v2
    msgs, meta = mod.build_messages(pk, spec["condition"], ranking)
    assert meta["case_text_sha256"] == out["prompt_meta"]["case_text_sha256"]
    return msgs


# ---------------------------------------------------------------- analysis (evaluation side: reads ground truth)
FAIL_CLASS = {"V0": "unit_or_schema_misuse", "V1": "unknown_citation", "V2": "out_of_window_citation",
              "V3": "subject_association_error", "V4": "value_miscopied", "V5": "not_above_baseline"}


def _gt(case_id):
    return next(c["gt"] for c in dev_cases() if c["case_id"] == case_id)


def _v1_assoc(c, rec_by_alias):
    """Post-hoc: would a p4a-v1 claim pass p4a-v2's association rules (ownership/record type only; no units)."""
    pred, subj = c.get("predicate"), c.get("subject_service")
    recs = [rec_by_alias[e] for e in c.get("evidence", []) if e in rec_by_alias]
    if pred not in output_schema_v2.TYPES:
        return ["unknown predicate"]
    if c.get("kind") == "inferred":
        return [f"premise not associated with {subj}" for r in recs if not (r["service"] == subj or r.get("peer_service") == subj)]
    if pred == "onset_before":
        o = c.get("object_service")
        return [] if len(recs) >= 2 and any(r["service"] == subj or r.get("peer_service") == subj for r in recs) \
            and any(r["service"] == o and r.get("peer_service") != subj for r in recs) else ["onset association"]
    return [b for r in recs if (b := output_schema_v2._owned(r, pred, subj))]


def _claim_rows(rec, gt):
    out = rec["output"]
    if not out.get("called") or not out.get("verification"):
        return [], []
    ver = out["verification"]
    vmap = {c["claim_id"]: c for c in ver["claims"]}
    raw = (json.loads(rec["call_record"]["raw_text"]) if out["parse"]["status"] == "valid" else {})
    alias = out["alias_map"]
    view_recs = {}
    typed = {f"c{c['index'] + 1}": c for c in (out.get("typed_checks") or {}).get("claims", [])}
    roots = gt["primary_root_services"]
    rows = []
    for i, c in enumerate(out["diagnosis"]["factual_claims"]):
        v = vmap.get(c["claim_id"], {})
        fails = [k for k, x in (v.get("checks") or {}).items() if x == "fail"]
        rc = (raw.get("claims") or [])[i] if raw else {}
        if rec["version"] == "p4a-v2":
            tpass, treasons = typed[c["claim_id"]]["passed"], typed[c["claim_id"]]["reasons"]
        else:
            treasons = None
            tpass = None
        rows.append({"version": rec["version"], "condition": rec["condition"], "category": rec["view"]["category"],
                     "case": rec["view"]["case_id"][:8], "spec": rec["view"]["spec"], "k": rec["k"],
                     "claim_id": c["claim_id"], "kind": c["kind"], "predicate": c["predicate"],
                     "subject_service": c["subject_service"], "object_service": c["object_service"],
                     "subject_is_root": c["subject_service"] in roots, "value": c["value"], "unit": c["unit"],
                     "measure_declared": rc.get("measure"), "evidence_aliases": [e if isinstance(e, str) else e.get("id")
                                                                                 for e in rc.get("evidence", [])],
                     "evidence_ids": c["evidence_ids"],
                     "verifier_checks": v.get("checks"), "verifier_notes": v.get("notes"),
                     "verified": bool(v.get("verified")), "premises_valid": v.get("premises_valid"),
                     "first_failed_check": fails[0] if fails else None,
                     "failure_class": FAIL_CLASS.get(fails[0]) if fails else None,
                     "v4_value_matches_rendered_precision": _rendered_match(c, v),
                     "typed_passed": tpass, "typed_reasons": treasons,
                     "in_post_filter": any(p["claim_id"] == c["claim_id"] for p in out["claims"]["post_filter"])})
    edges = []
    tedges = (out.get("typed_checks") or {}).get("edges", [])
    for i, (e, p) in enumerate(zip(out["diagnosis"]["propagation_path"], ver["propagation"])):
        edges.append({"version": rec["version"], "condition": rec["condition"], "category": rec["view"]["category"],
                      "k": rec["k"], "edge": p["edge"], "V6": p["V6"], "evidence": p["evidence"],
                      "verified": p["V6"] == "pass" and p["evidence"] == "pass",
                      "reversed": p["V6"] == "fail" and f"{e['to']}->{e['from']}" in
                      {f"{a}->{b}" for a, b in _reverse_ok(rec)},
                      "typed_passed": tedges[i]["passed"] if i < len(tedges) else None,
                      "typed_reasons": tedges[i]["reasons"] if i < len(tedges) else None})
    return rows, edges


def _rendered_match(c, v) -> bool | None:
    """For a V4 failure: does the claimed value equal the recomputed value as the record table rendered it
    (traces 1 decimal, metrics 4 significant digits)? Then the miss is a display-precision artifact of the
    evidence pack, not a miscopied number."""
    if (v.get("checks") or {}).get("V4") != "fail" or c["value"] is None:
        return None
    got = (v.get("notes") or {}).get("V4_recomputed")
    if got is None:
        return False
    return any(abs(float(fmt.format(got)) - float(c["value"])) < 1e-9 for fmt in ("{:.1f}", "{:.4g}"))


def _reverse_ok(rec):
    """Propagation edges (root side -> dependent) that the observed graph would admit, from the prompt's graph."""
    msg = rec.get("user_message") or ""
    sec = msg.split("Observed call edges (caller -> callee):\n", 1)[1].split("\n\n", 1)[0] if "Observed call edges" in msg else ""
    out = set()
    for line in sec.splitlines():
        if "->" in line:
            a, b = line.split(" (")[0].split(" -> ")
            out.add((b.strip(), a.strip()))
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _summ(recs, claims, edges, gts):
    called = [r for r in recs if r["output"].get("called")]
    obs = [c for c in claims if c["kind"] == "observed"]
    inf = [c for c in claims if c["kind"] == "inferred"]
    fault = [r for r in recs if gts[r["view"]["case_id"]]["primary_root_services"]]
    ctrl = [r for r in recs if not gts[r["view"]["case_id"]]["primary_root_services"]]
    roots = lambda r: gts[r["view"]["case_id"]]["primary_root_services"]
    ftype = lambda r: gts[r["view"]["case_id"]]["fault_type"]
    llm = lambda r: r["output"].get("llm") or {}
    cites = [r["output"].get("citations") or {} for r in called]
    n_cited = sum(c.get("n_cited", 0) for c in cites)
    fails = {}
    for c in obs:
        if c["failure_class"]:
            fails[c["failure_class"]] = fails.get(c["failure_class"], 0) + 1
    calls = [r["call_record"] for r in called]
    is_b4 = recs and recs[0]["condition"] == "B4"
    # diagnosis-level decision: B4 = the model's; E1 = E0's. LLM's own decision: B4 = same; E1 = recommendation
    own_dec = lambda r: llm(r).get("decision") if r["output"].get("called") else "ABSTAIN"
    return {
        "views": len(recs), "called": len(called), "not_called": len(recs) - len(called),
        "outcomes": {k: sum(c["outcome"] == k for c in calls) for k in ("ok", "timeout", "model_failure")},
        "parse": {k: sum((r["output"].get("parse") or {}).get("status") == k for r in called)
                  for k in ("valid", "schema_noncompliant", "malformed_json", "no_response")},
        "truncated_at_token_limit": sum(bool(c.get("truncated_at_token_limit")) for c in calls),
        "context_overflow_risk": sum(bool(c.get("context_overflow_risk")) for c in calls),
        "semantic_issues": sum(len(r["output"].get("semantic_issues") or []) for r in called),
        "claims_raw": len(claims), "observed_raw": len(obs), "inferred_raw": len(inf),
        "observed_verified": sum(c["verified"] for c in obs),
        "observed_verified_rate": round(sum(c["verified"] for c in obs) / len(obs), 3) if obs else None,
        "observed_verified_per_call": round(sum(c["verified"] for c in obs) / len(called), 3) if called else None,
        "observed_about_true_root_verified": sum(c["verified"] and c["subject_is_root"] for c in obs),
        "typed_rejected": sum(c["typed_passed"] is False for c in claims),
        "post_filter": sum(c["in_post_filter"] for c in claims),
        "inferred_premises_valid": sum(bool(c["premises_valid"]) for c in inf),
        "failed_observed_by_first_check": fails,
        "citations": {"n_cited": n_cited, "known_alias": sum(c.get("n_known_alias", 0) for c in cites),
                      "rate_known_alias": round(sum(c.get("n_known_alias", 0) for c in cites) / n_cited, 4) if n_cited else None,
                      "ownership_declared_correct": sum(c.get("n_ownership_correct", 0) for c in cites)
                      if any("n_ownership_correct" in c for c in cites) else None},
        "edges_raw": len(edges), "edges_verified": sum(e["verified"] for e in edges),
        "edges_reversed": sum(e["reversed"] for e in edges),
        "edges_typed_rejected": sum(e["typed_passed"] is False for e in edges),
        "verifier_flag_diagnose_without_verified_suspect_claim": sum(
            bool((r["output"].get("verification") or {}).get("flags")) for r in called),
        "decisions": {"DIAGNOSE": sum(r["output"].get("decision") == "DIAGNOSE" for r in recs),
                      "ABSTAIN": sum(r["output"].get("decision") == "ABSTAIN" for r in recs)},
        "llm_own_decisions": {"DIAGNOSE": sum(own_dec(r) == "DIAGNOSE" for r in called),
                              "ABSTAIN": sum(own_dec(r) == "ABSTAIN" for r in called)},
        "evidence_strength": {s: sum(llm(r).get("evidence_strength") == s for r in called)
                              for s in output_schema_v2.STRENGTH} if any("evidence_strength" in llm(r) for r in called) else None,
        "fault_views": len(fault), "control_views": len(ctrl),
        "control_false_diagnoses_decision": sum(r["output"].get("decision") == "DIAGNOSE" for r in ctrl),
        "control_false_diagnoses_llm_own": sum(own_dec(r) == "DIAGNOSE" for r in ctrl if r["output"].get("called")),
        "fault_top1_decision": sum(r["output"].get("decision") == "DIAGNOSE" and
                                   (r["output"].get("diagnosis") or {}).get("suspected_service") in roots(r) for r in fault),
        "fault_top1_llm_own": sum(own_dec(r) == "DIAGNOSE" and llm(r).get("suspected_service") in roots(r) for r in fault),
        "fault_llm_suspect_correct_any_decision": sum(llm(r).get("suspected_service") in roots(r) for r in fault),
        "fault_abstained_llm_own": sum(own_dec(r) == "ABSTAIN" for r in fault if r["output"].get("called")),
        "fault_type_correct_llm_own": sum(llm(r).get("suspected_fault_type") == ftype(r) for r in fault),
        "fault_type_correct_decision": sum(((r["output"].get("diagnosis") or {}).get("suspected_fault_type") == ftype(r))
                                           for r in fault),
        "e1_llm_disagrees_with_e0_top1": None if is_b4 else sum(llm(r).get("agrees_with_e0_top1") is False for r in called),
        "latency": {"wall_s_mean": _mean([c["wall_s"] for c in calls]),
                    "wall_s_median": round(statistics.median([c["wall_s"] for c in calls]), 2) if calls else None,
                    "wall_s_max": max((c["wall_s"] for c in calls), default=None),
                    "prompt_tokens_mean": _mean([c["prompt_tokens"] for c in calls]),
                    "output_tokens_mean": _mean([c["output_tokens"] for c in calls]),
                    "output_tok_per_s_mean": _mean([c.get("output_tokens_per_s") for c in calls]),
                    "prompt_tokens_max": max((c["prompt_tokens"] or 0 for c in calls), default=None)},
    }


def _load_runs():
    plan = json.loads(PLAN.read_text())
    recs = [json.loads(p.read_text()) for p in sorted(RAW.glob("*.json"))]
    return plan, recs


def analyze() -> int:
    plan, recs = _load_runs()
    gts = {r["case_id"]: _gt(r["case_id"]) for r in plan["views"]}
    claims, edges = [], []
    for r in recs:
        c, e = _claim_rows(r, gts[r["view"]["case_id"]])
        claims += c
        edges += e
    by = lambda xs, **kw: [x for x in xs if all(x.get(k) == v for k, v in kw.items())]
    rby = lambda **kw: [r for r in recs if all((r["view"][k] if k == "category" else r[k]) == v for k, v in kw.items())]
    cats = ["control", "straightforward", "cascade", "missing_trace", "conflicting"]
    groups = {}
    for v in ("p4a-v1", "p4a-v2"):
        for cond in ("B4", "E1"):
            g = {"all": _summ(rby(version=v, condition=cond), by(claims, version=v, condition=cond),
                              by(edges, version=v, condition=cond), gts)}
            for cat in cats:
                g[cat] = _summ(rby(version=v, condition=cond, category=cat),
                               by(claims, version=v, condition=cond, category=cat),
                               by(edges, version=v, condition=cond, category=cat), gts)
            groups[f"{v}/{cond}"] = g
    # paired per (view, condition)
    paired = []
    for row in plan["views"]:
        for cond in ("B4", "E1"):
            pair = {v: next((r for r in recs if r["view"]["index"] == row["index"] and r["condition"] == cond
                             and r["version"] == v), None) for v in ("p4a-v1", "p4a-v2")}
            if not all(pair.values()):
                continue
            ent = {"view_index": row["index"], "category": row["category"], "scenario_key": row["scenario_key"],
                   "spec": row["spec"], "condition": cond, "alert_fired": pair["p4a-v1"]["alert_fired"]}
            for v, r in pair.items():
                o = r["output"]
                cl = by(claims, version=v, condition=cond, k=r["k"])
                obs = [c for c in cl if c["kind"] == "observed"]
                ent[v] = {"called": o.get("called"), "parse": (o.get("parse") or {}).get("status"),
                          "decision": o.get("decision"), "llm_decision": (o.get("llm") or {}).get("decision"),
                          "llm_suspect": (o.get("llm") or {}).get("suspected_service"),
                          "llm_fault_type": (o.get("llm") or {}).get("suspected_fault_type"),
                          "evidence_strength": (o.get("llm") or {}).get("evidence_strength"),
                          "observed": len(obs), "observed_verified": sum(c["verified"] for c in obs),
                          "inferred": len(cl) - len(obs), "edges": len(by(edges, version=v, condition=cond, k=r["k"])),
                          "edges_verified": sum(e["verified"] for e in by(edges, version=v, condition=cond, k=r["k"])),
                          "wall_s": (r["call_record"] or {}).get("wall_s"),
                          "tokens": [(r["call_record"] or {}).get("prompt_tokens"), (r["call_record"] or {}).get("output_tokens")],
                          "case_text_sha256": (o.get("prompt_meta") or {}).get("case_text_sha256")}
            ent["same_evidence_text"] = ent["p4a-v1"]["case_text_sha256"] == ent["p4a-v2"]["case_text_sha256"]
            gt = gts[row["case_id"]]
            ent["truth"] = {"roots": gt["primary_root_services"], "fault_type": gt["fault_type"]}
            paired.append(ent)
    # reproducibility against the Phase 4A five-call benchmark (identical requests must give identical text)
    repro = []
    bench = Path("results/phase4a/raw")
    for f in sorted(bench.glob("call*.json")) if bench.exists() else []:
        b = json.loads(f.read_text())
        mine = [r for r in recs if r["version"] == "p4a-v1" and r["call_record"]
                and r["call_record"]["request_sha256"] == b["call_record"]["request_sha256"]]
        if mine:
            repro.append({"benchmark_call": f.name, "pilot_k": mine[0]["k"], "same_request": True,
                          "identical_raw_text": mine[0]["call_record"]["raw_text"] == b["call_record"]["raw_text"]})
    gen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    calls = [r["call_record"] for r in recs if r["call_record"]]
    meta = {"generated_at": gen, "split": "dev seed 11 only", "plan": str(PLAN), "n_records": len(recs),
            "model": {"name": "qwen3:4b", "digest": calls[0]["model_digest"] if calls else None,
                      "ollama_version": calls[0]["ollama_version"] if calls else None},
            "settings": calls[0]["settings"] if calls else None}
    dev = {"schema": "evidencerca.phase4a_v2.dev_comparison/v1", **meta, "groups": groups, "paired": paired,
           "reproducibility_vs_phase4a_benchmark": repro,
           "resources": {"ollama_rss_peak_mb": max((r["ollama_rss_peak_mb"] or 0) for r in recs) if recs else None,
                         "total_llm_wall_s": round(sum(c["wall_s"] or 0 for c in calls), 1),
                         "api_cost_usd": 0.0}}
    OUT.joinpath("dev_comparison.json").write_text(json.dumps(dev, indent=1, default=str) + "\n")
    agg = {}
    for v in ("p4a-v1", "p4a-v2"):
        for cond in ("B4", "E1"):
            cl = by(claims, version=v, condition=cond)
            obs = [c for c in cl if c["kind"] == "observed"]
            preds = sorted({c["predicate"] for c in obs})
            agg[f"{v}/{cond}"] = {
                "observed": len(obs), "verified": sum(c["verified"] for c in obs),
                "by_failure_class": {fc: sum(c["failure_class"] == fc for c in obs) for fc in FAIL_CLASS.values()},
                "by_predicate": {p: {"n": sum(c["predicate"] == p for c in obs),
                                     "verified": sum(c["verified"] for c in obs if c["predicate"] == p)} for p in preds},
                "v4_fail_value_matches_rendered_precision": sum(bool(c["v4_value_matches_rendered_precision"]) for c in obs),
                "check_fail_counts": {k: sum((c["verifier_checks"] or {}).get(k) == "fail" for c in obs)
                                      for k in ("V0", "V1", "V2", "V3", "V4", "V5")},
                "typed_gate": None if v == "p4a-v1" else {
                    "rejected": sum(c["typed_passed"] is False for c in cl),
                    "rejected_but_verifier_verified": sum(c["typed_passed"] is False and c["verified"] for c in obs),
                    "passed_but_verifier_rejected": sum(c["typed_passed"] is True and not c["verified"] for c in obs)},
                "edges": {"n": len(by(edges, version=v, condition=cond)),
                          "verified": sum(e["verified"] for e in by(edges, version=v, condition=cond)),
                          "reversed": sum(e["reversed"] for e in by(edges, version=v, condition=cond))}}
    OUT.joinpath("claim_analysis.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4a_v2.claim_analysis/v1", **meta,
         "note": "verifier verdicts are the frozen verifier's (V0-V6, unchanged); they are not human ground truth",
         "aggregate": agg, "claims": claims, "edges": edges}, indent=1, default=str) + "\n")
    ctrl_rows = []
    for r in recs:
        gt = gts[r["view"]["case_id"]]
        o = r["output"]
        ctrl_rows.append({"version": r["version"], "condition": r["condition"], "category": r["view"]["category"],
                          "scenario_key": r["view"]["scenario_key"], "spec": r["view"]["spec"],
                          "fault_present": bool(gt["primary_root_services"]), "alert_fired": r["alert_fired"],
                          "called": o.get("called"), "note": o.get("note"), "decision": o.get("decision"),
                          "llm_decision": (o.get("llm") or {}).get("decision"),
                          "llm_evidence_strength": (o.get("llm") or {}).get("evidence_strength"),
                          "llm_suspect": (o.get("llm") or {}).get("suspected_service"),
                          "e0_decision": (o.get("e0") or {}).get("decision"),
                          "uncertainty_reasons": (o.get("llm") or {}).get("uncertainty_reasons")})
    summ = {}
    for v in ("p4a-v1", "p4a-v2"):
        for cond in ("B4", "E1"):
            rows = [x for x in ctrl_rows if x["version"] == v and x["condition"] == cond]
            ctrl = [x for x in rows if not x["fault_present"]]
            flt = [x for x in rows if x["fault_present"] and x["called"]]
            summ[f"{v}/{cond}"] = {
                "control_views": len(ctrl), "control_called": sum(bool(x["called"]) for x in ctrl),
                "control_decision_DIAGNOSE": sum(x["decision"] == "DIAGNOSE" for x in ctrl),
                "control_llm_own_DIAGNOSE": sum(x["llm_decision"] == "DIAGNOSE" for x in ctrl),
                "control_llm_own_ABSTAIN": sum(x["llm_decision"] == "ABSTAIN" for x in ctrl),
                "fault_views_called": len(flt),
                "fault_llm_own_ABSTAIN": sum(x["llm_decision"] == "ABSTAIN" for x in flt),
                "conflicting_views_llm_own_ABSTAIN": sum(x["llm_decision"] == "ABSTAIN" for x in flt
                                                          if x["category"] == "conflicting")}
    OUT.joinpath("control_abstention.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4a_v2.control_abstention/v1", **meta,
         "definitions": {"control": "healthy dev case (no root cause); the correct output is ABSTAIN",
                         "decision": "diagnosis-level decision: B4 = the model's; E1 = E0's (unchanged by the model)",
                         "llm_decision": "the model's own decision; for E1 its independent abstention recommendation"},
         "summary": summ, "rows": ctrl_rows}, indent=1, default=str) + "\n")
    audit(recs, claims, plan)
    print(json.dumps({k: {c: groups[k]["all"][c] for c in ("called", "observed_raw", "observed_verified",
                                                         "observed_verified_rate", "edges_raw", "edges_verified",
                                                         "control_false_diagnoses_llm_own", "fault_top1_llm_own",
                                                         "fault_type_correct_llm_own")} for k in groups}, indent=1))
    print("repro:", repro)
    return 0


# ---------------------------------------------------------------- blinded audit sheet
def _table_lines(msg: str) -> dict:
    sec = msg.split("Evidence records (alias [selection] | time | service | type | value | status):\n", 1)
    out = {}
    for line in (sec[1].split("\n\n", 1)[0] if len(sec) == 2 else "").splitlines():
        alias, _, rest = line.partition(" ")
        out[alias] = rest.split("] ", 1)[1] if "] " in rest else rest      # selection tag dropped (blinding)
    return out


def _summary_lines(msg: str, services) -> str:
    sec = msg.split("Per-service summary (baseline vs analysis window):\n", 1)
    lines = (sec[1].split("\n\n", 1)[0] if len(sec) == 2 else "").splitlines()
    return "\n".join(l for l in lines if l.split(" | ")[0] in services)


PRED_GROUP = {"latency_above_baseline": "latency/cpu/pool", "cpu_above_baseline": "latency/cpu/pool",
              "pool_wait_above_baseline": "latency/cpu/pool", "error_rate_above_baseline": "errors/calls/timeouts/logs",
              "call_failed": "errors/calls/timeouts/logs", "timeout_observed": "errors/calls/timeouts/logs",
              "log_error_pattern": "errors/calls/timeouts/logs", "no_telemetry_from": "silence",
              "onset_before": "onset"}
GROUP_MIN, CONTEXT_MAX = 4, 30


def _group(c):
    return "inferred" if c["kind"] == "inferred" else PRED_GROUP.get(c["predicate"], "other")


def _pick(pool, n, rng):
    """n claims from pool: at least GROUP_MIN per predicate group when available, the rest at random."""
    pool = sorted(pool, key=lambda c: (c["k"], c["claim_id"]))
    rng.shuffle(pool)
    out = []
    for g in sorted({_group(c) for c in pool}):
        out += [c for c in pool if _group(c) == g][:GROUP_MIN]
    out = out[:n]
    out += [c for c in pool if c not in out][:n - len(out)]
    return out


def _context(view, win, c, rng):
    """Same-family records of the claimed service(s) in the baseline and analysis windows (seeded sample)."""
    from phase4.agent.evidence_pack import render_record
    fams = set()
    for eid in c["evidence_ids"]:
        r = view.by_id.get(eid)
        if r is None:
            continue
        rt = output_schema_v2.record_type(r)
        fams.add((rt, r["measurement"], r["peer_service"] if rt == "call" else r["service"]))
    out = {}
    for name, (t0, t1) in (("baseline", win.baseline), ("analysis", win.analysis)):
        recs = [r for r in view.records if t0 <= r["ts_start"] < t1 and
                (output_schema_v2.record_type(r), r["measurement"],
                 r["peer_service"] if output_schema_v2.record_type(r) == "call" else r["service"]) in fams]
        recs = sorted(recs, key=lambda r: r["evidence_id"])
        if len(recs) > CONTEXT_MAX:
            recs = sorted(rng.sample(recs, CONTEXT_MAX), key=lambda r: r["ts_start"])
        out[name] = "\n".join(render_record(r, win.alert_ns).split(" | ", 1)[1] for r in recs) or "(none)"
    return out


def audit(recs, claims, plan=None, target=AUDIT_TARGET) -> dict:
    """Blinded sheet (instrument dry run of docs/llm_evaluation_protocol.md section 9 on dev pilot claims):
    balanced over the four (version, condition) strata; within each, half verifier-verified and half rejected
    (shortfall filled from the other half and recorded); at least GROUP_MIN claims per predicate group when
    available. Seeded (AUDIT_SEED). Annotators see a canonical rendering, the cited records, same-family context
    records and the call edges -- never the model's wording, method, version, verifier verdict or labels."""
    rng = random.Random(AUDIT_SEED)
    strata = [(v, c) for v in ("p4a-v1", "p4a-v2") for c in ("B4", "E1")]
    per = -(-target // len(strata))
    chosen, shortfall = [], {}
    for v, cond in strata:
        pool = [c for c in claims if c["version"] == v and c["condition"] == cond]
        ver = [c for c in pool if c["verified"]]
        rej = [c for c in pool if not c["verified"]]
        nv = min(len(ver), max(per // 2, per - len(rej)))
        nr = min(len(rej), per - nv)
        shortfall[f"{v}/{cond}"] = {"verified_available": len(ver), "rejected_available": len(rej),
                                    "taken_verified": nv, "taken_rejected": nr}
        chosen += _pick(ver, nv, rng) + _pick(rej, nr, rng)
    rng.shuffle(chosen)
    rec_by_k = {r["k"]: r for r in recs}
    rows_by_index = {r["index"]: r for r in (plan or {}).get("views", [])}
    views, items = {}, {}
    aids = {id(c): f"A{i + 1:03d}" for i, c in enumerate(chosen)}      # ids follow the shuffled order
    for c in sorted(chosen, key=lambda c: (rec_by_k[c["k"]]["view"]["index"], aids[id(c)])):   # one view load each
        r = rec_by_k[c["k"]]
        vi = r["view"]["index"]
        if vi not in views:
            views.clear()
            views[vi] = load_view(rows_by_index.get(vi, r["view"]))
        view, win, _ = views[vi]
        msg = r.get("user_message") or ""
        table = _table_lines(msg)
        alias_rev = {v: k for k, v in r["output"]["alias_map"].items()}
        cited = "\n".join(table.get(alias_rev.get(e, ""), f"{e}: NOT IN THE RECORD TABLE") for e in c["evidence_ids"])
        aid = aids[id(c)]
        txt = (f"{c['kind'].upper()}: {c['predicate']}(subject={c['subject_service']}"
               + (f", object={c['object_service']}" if c["object_service"] else "") + ")"
               + (f" = {c['value']} {c['unit']}" if c["kind"] == "observed" else " [causal hypothesis; judge premises only]"))
        ctx = _context(view, win, c, random.Random(f"{AUDIT_SEED}|{aid}"))
        edges = msg.split("Observed call edges (caller -> callee):\n", 1)[1].split("\n\n", 1)[0] \
            if "Observed call edges" in msg else ""
        items[aid] = ({"audit_id": aid, "context_id": hashlib.sha256(f"{AUDIT_SEED}|{r['view_id']}".encode()).hexdigest()[:10],
                       "windows": msg.split("\n", 1)[0] if msg else "", "claim": txt, "cited_records": cited,
                       "context_baseline_same_family": ctx["baseline"], "context_analysis_same_family": ctx["analysis"],
                       "observed_call_edges": edges, "label": "", "partial_reason": "", "notes": ""},
                      {"audit_id": aid, "version": c["version"], "condition": c["condition"], "category": c["category"],
                    "predicate_group": _group(c), "k": c["k"], "claim_id": c["claim_id"], "view_id": r["view_id"],
                    "case": c["case"], "verifier_verified": c["verified"], "verifier_checks": c["verifier_checks"],
                    "typed_passed": c["typed_passed"]})
    sheet = [items[a][0] for a in sorted(items)]
    key = [items[a][1] for a in sorted(items)]
    d = OUT / "audit"
    d.mkdir(exist_ok=True)
    fields = ["audit_id", "context_id", "windows", "claim", "cited_records", "context_baseline_same_family",
              "context_analysis_same_family", "observed_call_edges", "label", "partial_reason", "notes"]
    with (d / "audit_sheet_dev_pilot.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sheet)
    (d / "audit_key_dev_pilot.SEALED.json").write_text(json.dumps(
        {"warning": "do not open before both annotators have submitted their labels", "seed": AUDIT_SEED,
         "rows": key}, indent=1, default=str) + "\n")
    counts = {f"{v}/{c}": sum(k["version"] == v and k["condition"] == c for k in key) for v, c in strata}
    groups = {}
    for k in key:
        groups[k["predicate_group"]] = groups.get(k["predicate_group"], 0) + 1
    (d / "audit_manifest.json").write_text(json.dumps(
        {"purpose": "dev-pilot dry run of the blinded audit instrument (protocol section 9); also the pool for the "
                    "rubric's worked examples. Not the Phase 4B audit sample.",
         "n_claims": len(sheet), "target": target, "per_stratum": counts, "stratum_fill": shortfall,
         "per_predicate_group": groups, "context_records_max_per_window": CONTEXT_MAX,
         "verifier_verified_share": round(sum(k["verifier_verified"] for k in key) / len(key), 3) if key else None,
         "labels": ["supported", "unsupported", "partially_supported", "not_assessable"],
         "blinded_fields_removed": ["prompt version", "condition", "selection tags", "LLM wording and explanation",
                                    "verifier verdict and statistics", "typed-check verdict", "case and view ids",
                                    "ground truth"]}, indent=1) + "\n")
    return counts


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan":
        OUT.mkdir(parents=True, exist_ok=True)
        plan = make_plan()
        PLAN.write_text(json.dumps(plan, indent=1) + "\n")
        for r in plan["views"]:
            print(r["index"], r["category"], r["scenario_key"], r["spec"], r["version_order"])
        print(plan["n_calls_max"], "calls max")
        return 0
    if cmd == "run":
        return run()
    if cmd == "analyze":
        return analyze()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
