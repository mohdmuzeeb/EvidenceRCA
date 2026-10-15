"""Phase 4B: locked evaluation of B4 and E1 (prompt p4a-v1, qwen3:4b) on the held-out test seeds 41/53/67;
E2 and B4-G are derived from the stored B4/E1 outputs without new model calls. Design: docs/phase4b_plan.md.

  uv run python -m phase4.eval.phase4b plan      # no model: matrix, call order, exact request hashes, leakage scan
  uv run python -m phase4.eval.phase4b run       # model calls, one at a time, resumable -> results/phase4b/raw/
  uv run python -m phase4.eval.phase4b repro     # predeclared determinism re-execution of 3 completed calls
  uv run python -m phase4.eval.phase4b analyze   # trials, claim metrics, comparisons, runtime summary
  uv run python -m phase4.eval.phase4b audit     # blinded audit package (protocol section 9.1, seed 20261101)

Predeclared matrix (fixed in code, committed with plan.json before any Phase 4B call):
  cases       every valid test-split case (evaluation.run_final.cases_for("test")): 47 faults + 6 controls,
              ordered by scenario key
  views       C0, R40 (perturbation seed 1), MT (traces removed), ML (logs removed), CF (decoy service);
              the evaluation views of the frozen protocol, built by evaluation.views.build
  window      the shared detector's alert window if it alerts, else a window anchored at the median record
              timestamp (label-free); identical for B4 and E1 on a view
  B4          ungated: one call per view (265)
  E1          one call per view where frozen E0 has a positive-score candidate on that window
  order       view by view in plan order; B4 first on even view indices, E1 first on odd ones
Ground truth is read only to build the evaluation views (CF needs the roots), never for prompts, and to score
outputs after the calls.
"""

import csv
import gzip
import hashlib
import json
import random
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evaluation.freeze import verify_frozen
from evaluation.run_final import cases_for, cluster_boot, holm, mcnemar_exact, wilson
from evaluation.views import build as build_view
from evidencerca import rca_input
from evidencerca.core import detect_alert, make_windows, observed_graph
from evidencerca.evidence import normalize
from evidencerca.methods.evidencerca_e0 import diagnose_full
from evidencerca.verifier import verify
from harness.leakage import scan_tokens
from phase4.agent import conditions, conditions_e2, evidence_pack, output_schema, prompts
from phase4.agent.evidence_pack import median_time, render_record
from phase4.agent.llm_client import LLMSettings, OllamaClient
from phase4.eval import pilot_v2
from phase4.eval.benchmark import Sampler, environment, system_memory

OUT = Path("results/phase4b")
RAW = OUT / "raw"
PLAN = OUT / "plan.json"
SPECS = [("C0", 1), ("R40", 1), ("MT", 1), ("ML", 1), ("CF", 1)]
REPRO_SEED, N_REPRO = 20261102, 3
AUDIT_SEED, AUDIT_PER_CONDITION, AUDIT_MAX_PER_CASE = 20261101, 80, 3
TRANSPORT_FAILURES = ("timeout", "model_failure")
MAX_TRANSPORT_ATTEMPTS = 2        # resumes re-attempt a transport failure at most once more


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def request_sha256(messages, settings: LLMSettings = LLMSettings()) -> str:
    """The exact request hash OllamaClient.chat computes (same payload, same serialization)."""
    payload = {"model": settings.model, "messages": messages, "format": output_schema.SCHEMA, "stream": False,
               "think": settings.think, "options": settings.options(), "keep_alive": settings.keep_alive}
    return _sha(json.dumps(payload, sort_keys=True))


def test_cases() -> list[dict]:
    return sorted(cases_for("test"), key=lambda c: c["gt"]["scenario"]["key"])


def window_for(view):
    win = detect_alert(view)
    fired = win.triggered
    if not fired:
        win = make_windows(view, median_time(view), "fixed-median", reason="label-free: median record timestamp")
    return win, fired


def _frozen_e0():
    """E0 alert-anchor trials of the frozen held-out evaluation: (case_id, spec, seed) -> output_sha256."""
    out = {}
    with gzip.open("results/final/test_trials.jsonl.gz", "rt") as f:
        for line in f:
            t = json.loads(line)
            if t["method"] == "E0" and t["anchor"] == "alert":
                out[(t["case_id"], t["condition"], t["perturbation_seed"])] = t
    return out


def _e0_sha(d) -> str:
    return _sha(json.dumps({k: v for k, v in d.items() if k != "_internal"}, sort_keys=True, default=str))[:16]


def _first_ids(e0):
    return e0["evidence_ids"] + [x for e in e0["propagation_path"] for x in e["evidence_ids"]]


# ---------------------------------------------------------------- plan (no model)
def make_plan() -> dict:
    frozen = _frozen_e0()
    views, calls, leak = [], [], []
    for case in test_cases():
        gt = case["gt"]
        clean = normalize(rca_input.load_case(case["case_id"]))
        for spec, seed in SPECS:
            view, _ = build_view(clean, case["case_id"], spec, seed, gt)
            win, fired = window_for(view)
            e0, _ = diagnose_full(view, win, use_graph=True)
            i = len(views)
            ft = frozen.get((case["case_id"], spec, seed))
            row = {"index": i, "case_id": case["case_id"], "scenario_key": gt["scenario"]["key"],
                   "seed": gt["scenario"]["master_seed"], "spec": spec, "perturbation_seed": seed,
                   "fault_present": bool(gt["primary_root_services"]), "view_id": view.view_id,
                   "alert_fired": fired, "window_anchor": win.anchor, "e0_decision": e0["decision"],
                   "e0_matches_frozen_test_output": (_e0_sha(e0) == ft["output_sha256"]) if fired and ft else None,
                   "order": ["B4", "E1"] if i % 2 == 0 else ["E1", "B4"]}
            views.append(row)
            for cond in row["order"]:
                ranking = e0 if cond == "E1" else None
                if cond == "E1" and e0["decision"] != "DIAGNOSE":
                    continue
                pk = evidence_pack.build(view, win, _first_ids(e0) if ranking else None)
                msgs, meta = prompts.build_messages(pk, cond, ranking)
                text = prompts.case_text_only(pk, ranking)
                scanned = "\n".join(l for l in text.splitlines() if not l.startswith("EvidenceRCA fault type:"))
                hits = scan_tokens(scanned.encode())
                gt_strings = [gt["scenario"]["key"], str(gt["timeline_vm_ns"].get("injection_start_ns")),
                              str(gt["timeline_vm_ns"].get("planned_injection_start_ns"))]
                bad = {"forbidden_token_hits": hits, "gt_strings_present": [g for g in gt_strings if g in text],
                       "case_id_present": case["case_id"] in text or case["case_id"][:8] in text}
                if hits or bad["gt_strings_present"] or bad["case_id_present"]:
                    leak.append({"view_index": i, "condition": cond, **bad})
                calls.append({"k": len(calls), "view_index": i, "condition": cond,
                              "request_sha256": request_sha256(msgs), "case_text_sha256": meta["case_text_sha256"],
                              "system_sha256": meta["system_sha256"], "task_sha256": meta["task_sha256"],
                              "n_records": meta["n_records"], "prompt_chars": sum(len(m["content"]) for m in msgs)})
        print(f"planned {case['gt']['scenario']['key']}: {len(views)} views, {len(calls)} calls", flush=True)
    by_view = defaultdict(dict)
    for c in calls:
        by_view[c["view_index"]][c["condition"]] = c["n_records"]
    s = LLMSettings()
    return {"schema": "evidencerca.phase4b.plan/v1", "split": "test seeds 41/53/67 (held-out; E0 results already seen)",
            "matrix_rule": __doc__.split("Predeclared matrix")[1].split("Ground truth is read")[0].strip(),
            "prompt_version": prompts.PROMPT_VERSION, "settings": s.__dict__,
            "prompt_system_sha256": _sha(prompts.SYSTEM), "output_schema_sha256": _sha(json.dumps(output_schema.SCHEMA,
                                                                                                    sort_keys=True)),
            "n_cases": len({v["case_id"] for v in views}),
            "n_fault_cases": len({v["case_id"] for v in views if v["fault_present"]}),
            "n_control_cases": len({v["case_id"] for v in views if not v["fault_present"]}),
            "n_views": len(views), "n_calls": len(calls),
            "n_calls_by_condition": dict(Counter(c["condition"] for c in calls)),
            "views_alerted": sum(v["alert_fired"] for v in views),
            "e0_reproduces_frozen_on_alerted_views": [sum(v["e0_matches_frozen_test_output"] is True for v in views),
                                                       sum(v["e0_matches_frozen_test_output"] is not None for v in views)],
            "equal_record_budget_B4_E1": all(len(set(d.values())) == 1 for d in by_view.values()),
            "leakage": {"prompts_scanned": len(calls), "violations": leak, "passed": not leak},
            "views": views, "calls": calls}


# ---------------------------------------------------------------- run (model calls)
def _raw_path(c, row):
    return RAW / f"{c['k']:03d}_v{row['index']:03d}_{row['spec']}_{c['condition']}.json"


def _preflight() -> str | None:
    env = environment()
    if env["colima_running"]:
        return "Colima is running"
    if "AC Power" not in (env["power"] or ""):
        return f"not on AC power: {env['power']}"
    if env["disk_free_gb"] < 5:
        return f"disk free {env['disk_free_gb']} GB < 5 GB"
    return None


def _load(row, gt, cache={}):
    cid = row["case_id"]
    if cache.get("cid") != cid:
        cache.clear()
        cache.update(cid=cid, clean=normalize(rca_input.load_case(cid)))
    view, _ = build_view(cache["clean"], cid, row["spec"], row["perturbation_seed"], gt)
    win, fired = window_for(view)
    assert view.view_id == row["view_id"] and fired == row["alert_fired"]
    return view, win


def _execute(c, row, gt, client) -> dict:
    view, win = _load(row, gt)
    sampler = Sampler()
    sampler.start()
    t0 = time.perf_counter()
    out = conditions.run(c["condition"], view, win, client)
    wall = time.perf_counter() - t0
    peak = sampler.stop()
    call = out.pop("call", None)
    assert out["called"] and call is not None, "a planned call was not made"
    if call["request_sha256"] != c["request_sha256"] or out["prompt_meta"]["case_text_sha256"] != c["case_text_sha256"]:
        raise RuntimeError(f"call {c['k']}: request differs from the committed plan")
    ranking = None
    if c["condition"] == "E1":
        ranking, _ = diagnose_full(view, win, use_graph=True)
    pk = evidence_pack.build(view, win, _first_ids(ranking) if ranking else None)
    msgs, _ = prompts.build_messages(pk, c["condition"], ranking)
    return {"k": c["k"], "condition": c["condition"], "view": row, "view_id": view.view_id,
            "planned_request_sha256": c["request_sha256"], "end_to_end_wall_s": round(wall, 2),
            "ollama_rss_peak_mb": peak, "system_after": system_memory(), "call_record": call, "output": out,
            "user_message": msgs[1]["content"]}


def _status(path) -> str:
    if not path.exists():
        return "todo"
    rec = json.loads(path.read_text())
    return "transport_failure" if rec["call_record"]["outcome"] in TRANSPORT_FAILURES else "done"


def run() -> int:
    verify_frozen()
    plan = json.loads(PLAN.read_text())
    if why := _preflight():
        print(why, file=sys.stderr)
        return 2
    client = OllamaClient(LLMSettings())
    if client.digest is None:
        print("qwen3:4b not available on the local Ollama server", file=sys.stderr)
        return 2
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / "failed").mkdir(exist_ok=True)
    (OUT / "run_logs").mkdir(exist_ok=True)
    (OUT / "run_logs" / f"environment_before_{int(time.time())}.json").write_text(json.dumps(
        {"environment": environment(), "memory": system_memory(), "model_digest": client.digest,
         "ollama_version": client.version}, indent=1) + "\n")
    rows = {r["index"]: r for r in plan["views"]}
    gts = {c["case_id"]: c["gt"] for c in test_cases()}
    todo = [c for c in plan["calls"] if _status(_raw_path(c, rows[c["view_index"]])) != "done"]
    print(f"{len(plan['calls']) - len(todo)} calls already complete, {len(todo)} to run", flush=True)
    t_all, n = time.time(), 0
    for c in todo:
        row = rows[c["view_index"]]
        path = _raw_path(c, row)
        prior = sorted((RAW / "failed").glob(path.stem + ".attempt*.json"))
        if path.exists():                       # a transport failure from an earlier session: archive, re-attempt
            if len(prior) + 1 >= MAX_TRANSPORT_ATTEMPTS:
                continue
            path.rename(RAW / "failed" / f"{path.stem}.attempt{len(prior) + 1}.json")
            prior = sorted((RAW / "failed").glob(path.stem + ".attempt*.json"))
        if why := _preflight():
            print(f"stopping before call {c['k']}: {why}", file=sys.stderr, flush=True)
            client.unload()
            return 3
        rec = _execute(c, row, gts[row["case_id"]], client)
        rec["earlier_transport_failures"] = [p.name for p in prior]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1, default=str) + "\n")
        tmp.rename(path)                        # atomic: a call file exists only once it is complete
        n += 1
        cr, o = rec["call_record"], rec["output"]
        print(f"[{c['k'] + 1}/{len(plan['calls'])}] {row['scenario_key']:40s} {row['spec']:3s} {c['condition']} "
              f"outcome={cr['outcome']} parse={(o.get('parse') or {}).get('status')} wall={cr['wall_s']:.1f}s "
              f"tok={cr['prompt_tokens']}/{cr['output_tokens']} decision={o.get('decision')} "
              f"rss={rec['ollama_rss_peak_mb']} eta={(time.time() - t_all) / n * (len(todo) - n) / 3600:.2f}h",
              flush=True)
    client.unload()
    print(f"run done: {n} calls in {time.time() - t_all:.0f}s")
    return 0


def repro() -> int:
    """Predeclared determinism check: re-execute N_REPRO completed calls (seeded choice) and compare raw text."""
    plan = json.loads(PLAN.read_text())
    rows = {r["index"]: r for r in plan["views"]}
    gts = {c["case_id"]: c["gt"] for c in test_cases()}
    picks = sorted(random.Random(REPRO_SEED).sample(range(len(plan["calls"])), N_REPRO))
    if why := _preflight():
        print(why, file=sys.stderr)
        return 2
    client = OllamaClient(LLMSettings())
    d = OUT / "raw_repro"
    d.mkdir(exist_ok=True)
    res = []
    for k in picks:
        c = plan["calls"][k]
        orig = json.loads(_raw_path(c, rows[c["view_index"]]).read_text())
        rec = _execute(c, rows[c["view_index"]], gts[rows[c["view_index"]]["case_id"]], client)
        (d / f"{k:03d}_{c['condition']}.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
        res.append({"k": k, "condition": c["condition"], "same_request": rec["call_record"]["request_sha256"] ==
                    orig["call_record"]["request_sha256"],
                    "identical_raw_text": rec["call_record"]["raw_text"] == orig["call_record"]["raw_text"],
                    "wall_s": [orig["call_record"]["wall_s"], rec["call_record"]["wall_s"]]})
        print(res[-1], flush=True)
    client.unload()
    (OUT / "repro_check.json").write_text(json.dumps({"seed": REPRO_SEED, "calls": res,
                                                      "all_identical": all(r["identical_raw_text"] for r in res)},
                                                     indent=1) + "\n")
    return 0


# ---------------------------------------------------------------- analysis (evaluation side: reads ground truth)
FAIL_CLASS = pilot_v2.FAIL_CLASS


def _load_records(plan):
    rows = {r["index"]: r for r in plan["views"]}
    recs = {}
    for c in plan["calls"]:
        p = _raw_path(c, rows[c["view_index"]])
        if p.exists():
            recs[c["k"]] = json.loads(p.read_text())
    return recs


def _claim_rows(rec, gt, condition):
    """Per-claim rows (frozen verifier verdicts), reusing the Phase 4A-v2 row builder for p4a-v1 records."""
    shim = {"k": rec["k"], "version": "p4a-v1", "condition": rec["condition"], "output": rec["output"],
            "call_record": rec["call_record"], "user_message": rec["user_message"],
            "view": {"category": rec["view"]["spec"], "case_id": rec["view"]["case_id"], "spec": rec["view"]["spec"]}}
    rows, edges = pilot_v2._claim_rows(shim, gt)
    cited = {c["claim_id"]: c for c in rec["output"]["diagnosis"]["factual_claims"]} if rec["output"].get("diagnosis") else {}
    for r in rows:
        r.update(condition=condition, case_id=rec["view"]["case_id"], view_index=rec["view"]["index"],
                 subject_in_symptoms=r["subject_service"] in (gt.get("propagated_symptom_services") or []),
                 citations_all_known=all(e in rec["output"]["alias_map"] for e in r["evidence_aliases"]) and
                 bool(r["evidence_aliases"]))
        r["off_target"] = not r["subject_is_root"] and not r["subject_in_symptoms"]
        r["citations_all_in_view"] = bool(cited[r["claim_id"]]["evidence_ids"]) and all(
            e.startswith("ev-") for e in cited[r["claim_id"]]["evidence_ids"])
    for e in edges:
        e.update(condition=condition, case_id=rec["view"]["case_id"], view_index=rec["view"]["index"])
    return rows, edges


def _e0_claim_rows(view, win, row, gt):
    """Frozen E0 (reference) on the alert window, exactly as in the frozen held-out evaluation."""
    if not row["alert_fired"]:
        return None, [], []
    e0, signals = diagnose_full(view, win, use_graph=True)
    if e0["decision"] != "DIAGNOSE":
        return e0, [], []
    ver = verify(e0, view)
    vmap = {c["claim_id"]: c for c in ver["claims"]}
    roots = gt["primary_root_services"]
    rows = []
    for c in e0["factual_claims"]:
        v = vmap.get(c["claim_id"], {})
        fails = [k for k, x in (v.get("checks") or {}).items() if x == "fail"]
        rows.append({"condition": "E0", "case_id": row["case_id"], "view_index": row["index"], "spec": row["spec"],
                     "k": f"E0-{row['index']}", "claim_id": c["claim_id"], "kind": c["kind"], "predicate": c["predicate"],
                     "subject_service": c["subject_service"], "object_service": c["object_service"],
                     "subject_is_root": c["subject_service"] in roots,
                     "subject_in_symptoms": c["subject_service"] in (gt.get("propagated_symptom_services") or []),
                     "value": c["value"], "unit": c["unit"], "evidence_ids": c["evidence_ids"],
                     "verifier_checks": v.get("checks"), "verified": bool(v.get("verified")),
                     "premises_valid": v.get("premises_valid"), "first_failed_check": fails[0] if fails else None,
                     "failure_class": FAIL_CLASS.get(fails[0]) if fails else None})
        rows[-1]["off_target"] = not rows[-1]["subject_is_root"] and not rows[-1]["subject_in_symptoms"]
    edges = [{"condition": "E0", "case_id": row["case_id"], "view_index": row["index"], "edge": p["edge"],
              "V6": p["V6"], "evidence": p["evidence"], "verified": p["V6"] == "pass" and p["evidence"] == "pass",
              "reversed": None} for p in ver["propagation"]]
    return e0, rows, edges


def _trial(cond, row, gt, out, call=None, gate=None):
    """Diagnosis-level trial row. `out` is a conditions.run-style output (or a no-call ABSTAIN)."""
    roots = gt["primary_root_services"]
    out = out or {}
    d = out.get("diagnosis") or {}
    if cond in ("E1", "E1-frozen-gated", "E2") and out.get("e0"):
        d = out["e0"]           # by design E1/E2's diagnosis-level outcome is E0's, also when the LLM output is unusable
    llm = out.get("llm") or {}
    dec = d.get("decision") or out.get("decision") or "ABSTAIN"
    sus = d.get("suspected_service") if dec == "DIAGNOSE" else None
    ranked = [c["service"] for c in d.get("ranked_candidates", []) if c["score"] is None or c["score"] > 0] \
        if dec == "DIAGNOSE" else []
    return {"condition": cond, "view_index": row["index"], "case_id": row["case_id"], "scenario": row["scenario_key"],
            "seed": row["seed"], "spec": row["spec"], "fault_present": bool(roots), "fault_type": gt["fault_type"],
            "alert_fired": row["alert_fired"], "called": bool((out or {}).get("called")),
            "outcome": (call or {}).get("outcome"), "parse": ((out or {}).get("parse") or {}).get("status"),
            "decision": dec, "suspected_service": sus,
            "top1_correct": (sus in roots) if roots else None,
            "top3_correct": any(s in roots for s in ranked[:3]) if roots else None,
            "fault_type_correct": (d.get("suspected_fault_type") == gt["fault_type"]) if roots and dec == "DIAGNOSE" else
            (False if roots else None),
            "false_diagnosis_on_control": (dec == "DIAGNOSE") if not roots else None,
            "llm_decision": llm.get("decision"), "llm_suspect": llm.get("suspected_service"),
            "llm_suspect_correct": (llm.get("suspected_service") in roots) if roots and llm else None,
            "llm_agrees_with_e0": llm.get("agrees_with_e0_top1"), "gate": gate,
            "wall_s": (call or {}).get("wall_s"), "prompt_tokens": (call or {}).get("prompt_tokens"),
            "output_tokens": (call or {}).get("output_tokens")}


def _rate(k, n):
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def _per_case(trials, field):
    """Mean of a 0/1 field per base case (cases with at least one eligible trial)."""
    by = defaultdict(list)
    for t in trials:
        if t[field] is not None:
            by[t["case_id"]].append(1.0 if t[field] else 0.0)
    return {c: statistics.mean(v) for c, v in by.items()}


def _diag_summary(trials):
    fault = [t for t in trials if t["fault_present"]]
    ctrl = [t for t in trials if not t["fault_present"]]
    pc = _per_case(fault, "top1_correct")
    out = {"views": len(trials), "fault_views": len(fault), "control_views": len(ctrl),
           "fault_base_cases": len({t["case_id"] for t in fault}), "control_base_cases": len({t["case_id"] for t in ctrl}),
           "calls": sum(t["called"] for t in trials),
           "top1": _rate(sum(bool(t["top1_correct"]) for t in fault), len(fault)),
           "top1_case_mean": round(statistics.mean(pc.values()), 4) if pc else None,
           "top1_case_mean_cluster_boot95": cluster_boot(pc) if pc else None,
           "top3": _rate(sum(bool(t["top3_correct"]) for t in fault), len(fault)),
           "fault_type": _rate(sum(bool(t["fault_type_correct"]) for t in fault), len(fault)),
           "fault_abstained": _rate(sum(t["decision"] == "ABSTAIN" for t in fault), len(fault)),
           "wrong_service_diagnoses": sum(t["decision"] == "DIAGNOSE" and not t["top1_correct"] for t in fault),
           "control_false_diagnoses": _rate(sum(bool(t["false_diagnosis_on_control"]) for t in ctrl), len(ctrl)),
           "control_cases_with_any_false_diagnosis": len({t["case_id"] for t in ctrl if t["false_diagnosis_on_control"]}),
           "unusable_outputs": sum(t["called"] and t["parse"] != "valid" for t in trials)}
    return out


def _claim_summary(claims, edges, n_calls):
    obs = [c for c in claims if c["kind"] == "observed"]
    inf = [c for c in claims if c["kind"] == "inferred"]
    ver = [c for c in obs if c["verified"]]
    pcv = defaultdict(lambda: [0, 0])
    for c in obs:
        pcv[c["case_id"]][0] += c["verified"]
        pcv[c["case_id"]][1] += 1
    keys = sorted(pcv)

    def ratio(sample):
        n = sum(s[1] for s in sample)
        return sum(s[0] for s in sample) / n if n else 0.0
    boot = cluster_boot({k: pcv[k] for k in keys}, stat=ratio) if len(keys) > 1 else None
    fails = Counter(c["failure_class"] for c in obs if c["failure_class"])
    checks = {k: sum((c.get("verifier_checks") or {}).get(k) == "fail" for c in obs) for k in ("V0", "V1", "V2", "V3", "V4", "V5")}
    known = [c for c in obs if c.get("citations_all_known") is not None]
    return {
        "calls_with_claims_basis": n_calls,
        "claims_total": len(claims), "observed": len(obs), "inferred": len(inf),
        "observed_verified": len(ver), "observed_rejected": len(obs) - len(ver),
        "observed_verified_rate": _rate(len(ver), len(obs)),
        "observed_verified_rate_cluster_boot95": boot,
        "inferred_premises_valid": sum(bool(c["premises_valid"]) for c in inf),
        "retained_after_filtering": _rate(len(ver), len(claims)),
        "verified_per_call": round(len(ver) / n_calls, 3) if n_calls else None,
        "verified_about_true_root": sum(c["subject_is_root"] for c in ver),
        "observed_about_true_root": sum(c["subject_is_root"] for c in obs),
        "verified_off_target": sum(c["off_target"] for c in ver),
        "observed_off_target": sum(c["off_target"] for c in obs),
        "rejected_off_target": sum(c["off_target"] and not c["verified"] for c in obs),
        "citations": None if not known else {
            "claims_with_all_aliases_known": sum(bool(c["citations_all_known"]) for c in known),
            "claims_with_all_ids_in_view": sum(bool(c.get("citations_all_in_view")) for c in known),
            "valid_ids_but_rejected": sum(bool(c.get("citations_all_in_view")) and not c["verified"] for c in known)},
        "errors": {"service_ownership_V3": checks["V3"], "value_V4": checks["V4"],
                   "unit_or_schema_V0": checks["V0"], "unknown_citation_V1": checks["V1"],
                   "out_of_window_V2": checks["V2"], "not_above_baseline_V5": checks["V5"],
                   "v4_fail_matches_rendered_precision": sum(bool(c.get("v4_value_matches_rendered_precision")) for c in obs),
                   "by_first_failed_check": dict(fails)},
        "propagation_edges": {"n": len(edges), "verified": sum(e["verified"] for e in edges),
                              "incorrect": sum(not e["verified"] for e in edges),
                              "reversed": sum(bool(e.get("reversed")) for e in edges)},
        "by_predicate": {p: {"n": sum(c["predicate"] == p for c in obs),
                             "verified": sum(c["verified"] for c in obs if c["predicate"] == p)}
                         for p in sorted({c["predicate"] for c in obs})},
    }


def _paired(a_trials, b_trials, field="top1_correct"):
    """Paired comparison on matched fault views; per spec exact McNemar (each spec: one view per base case), pooled
    per-case mean difference with a cluster bootstrap over base cases."""
    A = {(t["case_id"], t["spec"]): t for t in a_trials if t["fault_present"]}
    B = {(t["case_id"], t["spec"]): t for t in b_trials if t["fault_present"]}
    keys = sorted(set(A) & set(B))
    per_spec, pvals = {}, {}
    for spec, _ in SPECS:
        ks = [k for k in keys if k[1] == spec]
        b = sum(bool(A[k][field]) and not B[k][field] for k in ks)
        c = sum(not A[k][field] and bool(B[k][field]) for k in ks)
        pvals[spec] = mcnemar_exact(b, c)
        per_spec[spec] = {"n_base_cases": len(ks), "a_correct": sum(bool(A[k][field]) for k in ks),
                          "b_correct": sum(bool(B[k][field]) for k in ks), "a_only": b, "b_only": c,
                          "mcnemar_exact_p": round(pvals[spec], 6)}
    for spec, p in holm(pvals).items():
        per_spec[spec]["holm_p"] = p
    diff = defaultdict(list)
    for k in keys:
        diff[k[0]].append(float(bool(A[k][field])) - float(bool(B[k][field])))
    pc = {c: statistics.mean(v) for c, v in diff.items()}
    return {"n_base_cases": len(pc), "n_views": len(keys), "per_spec": per_spec,
            "mean_case_difference_a_minus_b": round(statistics.mean(pc.values()), 4) if pc else None,
            "cluster_boot95": cluster_boot(pc) if pc else None}


def _paired_claims(a_claims, b_claims):
    """Added after the run (descriptive; disclosed in docs/phase4b_validation.md): per base case, verified rate of
    observed claims and verified root claims, A minus B, over base cases with observed claims under both."""
    def per_case(cl):
        d = defaultdict(lambda: [0, 0, 0])
        for c in cl:
            if c["kind"] == "observed":
                d[c["case_id"]][0] += c["verified"]
                d[c["case_id"]][1] += 1
                d[c["case_id"]][2] += c["verified"] and c["subject_is_root"]
        return d
    A, B = per_case(a_claims), per_case(b_claims)
    keys = sorted(set(A) & set(B))
    rate = {k: A[k][0] / A[k][1] - B[k][0] / B[k][1] for k in keys}
    root = {k: float(A[k][2] - B[k][2]) for k in keys}
    return {"n_base_cases": len(keys),
            "verified_rate_mean_case_difference": round(statistics.mean(rate.values()), 4) if keys else None,
            "verified_rate_cluster_boot95": cluster_boot(rate) if keys else None,
            "cases_a_higher": sum(v > 0 for v in rate.values()), "cases_b_higher": sum(v < 0 for v in rate.values()),
            "verified_root_claims_mean_case_difference": round(statistics.mean(root.values()), 4) if keys else None,
            "verified_root_claims_cluster_boot95": cluster_boot(root) if keys else None}


def analyze() -> int:
    plan = json.loads(PLAN.read_text())
    recs = _load_records(plan)
    rows = {r["index"]: r for r in plan["views"]}
    gts = {c["case_id"]: c["gt"] for c in test_cases()}
    by_view = defaultdict(dict)
    for r in recs.values():
        by_view[r["view"]["index"]][r["condition"]] = r
    trials = {c: [] for c in ("B4", "E1", "E1-frozen-gated", "E2", "B4-G", "E0")}
    claims = {c: [] for c in ("B4", "E1", "E2-canonical", "B4-G", "E0")}
    edges = {c: [] for c in ("B4", "E1", "B4-G", "E0")}
    own_rows, e0_repro, missing = [], [], []
    for i, row in sorted(rows.items()):
        gt = gts[row["case_id"]]
        view, win = _load(row, gt)
        b4, e1 = by_view[i].get("B4"), by_view[i].get("E1")
        # B4 (ungated), E1 (pilot window rule), E2 (E1 + ownership), B4-G (gate on the frozen detector)
        if b4:
            trials["B4"].append(_trial("B4", row, gt, b4["output"], b4["call_record"]))
            c, e = _claim_rows(b4, gt, "B4")
            claims["B4"] += c
            edges["B4"] += e
            if row["alert_fired"]:
                trials["B4-G"].append(_trial("B4-G", row, gt, b4["output"], b4["call_record"], gate="alert"))
                claims["B4-G"] += [dict(x, condition="B4-G") for x in c]
                edges["B4-G"] += [dict(x, condition="B4-G") for x in e]
        if not row["alert_fired"]:
            trials["B4-G"].append(_trial("B4-G", row, gt, {"decision": "ABSTAIN", "called": False}, gate="silent"))
        e1_out = e1["output"] if e1 else {"decision": "ABSTAIN", "called": False}
        if not e1 and row["e0_decision"] != "DIAGNOSE":      # planned no-call: E0 has no candidate here
            e0d, _ = diagnose_full(view, win, use_graph=True)
            assert e0d["decision"] != "DIAGNOSE"
        if not e1 and row["e0_decision"] == "DIAGNOSE" or not b4:
            missing.append({"view_index": i, "B4": bool(b4), "E1": bool(e1) or row["e0_decision"] != "DIAGNOSE"})
        trials["E1"].append(_trial("E1", row, gt, e1_out, e1["call_record"] if e1 else None))
        if e1:
            c, e = _claim_rows(e1, gt, "E1")
            claims["E1"] += c
            edges["E1"] += e
            e2 = conditions_e2.e2_from_e1(e1["output"], view)
            stored_ok = {x["claim_id"] for x in (e1["output"].get("verification") or {}).get("claims", []) if x["verified"]}
            re_ver = verify(e1["output"]["diagnosis"], view) if e1["output"].get("verification") else None
            assert {x["claim_id"] for x in (re_ver or {}).get("claims", []) if x["verified"]} == stored_ok
            trials["E2"].append(_trial("E2", row, gt, e1_out, e1["call_record"]))
            if e2.get("ownership"):
                cv = {x["claim_id"]: x for x in (e2.get("canonical_verification") or {}).get("claims", [])}
                for a in e2["ownership"]["assessments"]:
                    can = a.get("canonical")
                    v = cv.get(can["claim_id"], {}) if can else {}
                    own_rows.append({"view_index": i, "case_id": row["case_id"], "spec": row["spec"],
                                     "claim_id": a["claim_id"], "predicate": a["predicate"], "kind": a["kind"],
                                     "disposition": a["disposition"], "reason": a.get("reason"),
                                     "original_subject": a["original_subject"], "implied_subject": a.get("implied_subject"),
                                     "changes": (a.get("audit") or {}).get("changes"),
                                     "canonical_verified": bool(v.get("verified")) if can else None,
                                     "canonical_first_failed_check": next((k for k, x in (v.get("checks") or {}).items()
                                                                           if x == "fail"), None) if can else None,
                                     "canonical_subject_is_root": (can["subject_service"] in gt["primary_root_services"])
                                     if can else None})
                    if can:
                        claims["E2-canonical"].append({
                            "condition": "E2-canonical", "case_id": row["case_id"], "view_index": i, "spec": row["spec"],
                            "kind": "observed", "predicate": can["predicate"], "subject_service": can["subject_service"],
                            "verified": bool(v.get("verified")), "premises_valid": None,
                            "subject_is_root": can["subject_service"] in gt["primary_root_services"],
                            "subject_in_symptoms": can["subject_service"] in (gt.get("propagated_symptom_services") or []),
                            "verifier_checks": v.get("checks"),
                            "failure_class": FAIL_CLASS.get(next((k for k, x in (v.get("checks") or {}).items()
                                                                  if x == "fail"), None))})
                        claims["E2-canonical"][-1]["off_target"] = not claims["E2-canonical"][-1]["subject_is_root"] \
                            and not claims["E2-canonical"][-1]["subject_in_symptoms"]
        else:
            trials["E2"].append(_trial("E2", row, gt, e1_out))
        # E0 reference and the frozen gated reading of E1/E2 (no alert -> ABSTAIN, as in the frozen protocol)
        e0, c0, ed0 = _e0_claim_rows(view, win, row, gt)
        claims["E0"] += c0
        edges["E0"] += ed0
        e0_out = {"decision": e0["decision"] if e0 else "ABSTAIN", "called": False,
                  "diagnosis": e0 if e0 else None}
        trials["E0"].append(_trial("E0", row, gt, e0_out))
        gated = e1_out if row["alert_fired"] else {"decision": "ABSTAIN", "called": False}
        trials["E1-frozen-gated"].append(_trial("E1-frozen-gated", row, gt, gated, e1["call_record"] if e1 else None))
        if e0 is not None:
            e0_repro.append(row["e0_matches_frozen_test_output"])
        if i % 25 == 0:
            print(f"analyzed view {i + 1}/{len(rows)}", flush=True)

    n_calls = {"B4": sum(t["called"] for t in trials["B4"]), "E1": sum(t["called"] for t in trials["E1"]),
               "B4-G": sum(t["called"] for t in trials["B4-G"])}
    n_calls["E2-canonical"] = n_calls["E1"]
    n_calls["E0"] = sum(t["decision"] == "DIAGNOSE" for t in trials["E0"])     # E0 diagnoses (no LLM)
    gen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    calls = [r["call_record"] for r in recs.values()]
    meta = {"generated_at": gen, "split": plan["split"], "plan_sha256": _sha(PLAN.read_text()),
            "prompt_version": plan["prompt_version"], "n_planned_calls": plan["n_calls"], "n_completed_calls": len(recs),
            "views_with_missing_planned_calls": missing,
            "model": {"name": "qwen3:4b", "digests": sorted({c["model_digest"] for c in calls if c["model_digest"]}),
                      "ollama_versions": sorted({c["ollama_version"] for c in calls if c["ollama_version"]})},
            "note": "verifier verdicts are the frozen verifier's (V0-V6); they are not human ground truth"}

    # ---- trials file
    OUT.mkdir(exist_ok=True)
    with gzip.open(OUT / "llm_trials.jsonl.gz", "wt") as f:
        for k in sorted(recs):
            r = recs[k]
            f.write(json.dumps({"type": "llm_call", **{x: r[x] for x in ("k", "condition", "view", "view_id",
                                                                          "planned_request_sha256", "end_to_end_wall_s",
                                                                          "ollama_rss_peak_mb", "call_record", "output",
                                                                          "user_message")},
                                "earlier_transport_failures": r.get("earlier_transport_failures", [])},
                               default=str) + "\n")
        for cond, ts in trials.items():
            for t in ts:
                f.write(json.dumps({"type": "trial", **t}, default=str) + "\n")

    # ---- claim metrics
    by_spec = lambda xs, s: [x for x in xs if x["spec"] == s]
    by_fault = lambda xs, flt: [x for x in xs if bool(gts[x["case_id"]]["primary_root_services"]) == flt]
    cm = {}
    for cond in claims:
        tcond = {"E2-canonical": "E2"}.get(cond, cond)
        cm[cond] = {"all": _claim_summary(claims[cond], edges.get(cond, []), n_calls[cond]),
                    "fault_views": _claim_summary(by_fault(claims[cond], True), by_fault(edges.get(cond, []), True),
                                                  sum(t["called"] or (cond == "E0" and t["decision"] == "DIAGNOSE")
                                                      for t in trials[tcond] if t["fault_present"])),
                    "control_views": _claim_summary(by_fault(claims[cond], False), by_fault(edges.get(cond, []), False),
                                                    sum(t["called"] or (cond == "E0" and t["decision"] == "DIAGNOSE")
                                                        for t in trials[tcond] if not t["fault_present"])),
                    "by_spec": {s: _claim_summary(by_spec(claims[cond], s),
                                                  [e for e in edges.get(cond, []) if rows[e["view_index"]]["spec"] == s],
                                                  sum(t["called"] or (cond == "E0" and t["decision"] == "DIAGNOSE")
                                                      for t in trials[tcond] if t["spec"] == s)) for s, _ in SPECS}}
    disp = Counter(r["disposition"] for r in own_rows)
    obs_orig = sum(r["kind"] == "observed" for r in own_rows)
    canon = [r for r in own_rows if r["disposition"] == "transformed"]
    cm["E2_ownership"] = {
        "dispositions": dict(disp), "original_observed": obs_orig,
        "transformation_rate": _rate(disp["transformed"], obs_orig), "rejection_rate": _rate(disp["rejected"], obs_orig),
        "subject_ownership_errors": sum("subject_service" in (r["changes"] or {}) for r in canon),
        "unit_changes": sum("unit" in (r["changes"] or {}) for r in canon),
        "canonical_verified": _rate(sum(bool(r["canonical_verified"]) for r in canon), len(canon)),
        "canonical_about_true_root": sum(bool(r["canonical_subject_is_root"]) for r in canon),
        "canonical_verified_about_true_root": sum(bool(r["canonical_subject_is_root"]) and bool(r["canonical_verified"])
                                                  for r in canon),
        "canonical_failed_by_first_check": dict(Counter(r["canonical_first_failed_check"] for r in canon
                                                        if not r["canonical_verified"])),
        "rejected_reasons": dict(Counter((r["reason"] or "").split(" [")[0].split(" {")[0] for r in own_rows
                                         if r["disposition"] == "rejected")),
        "verified_per_call": {"original_only": cm["E1"]["all"]["verified_per_call"],
                              "original_plus_canonical": round((cm["E1"]["all"]["observed_verified"] +
                                                                sum(bool(r["canonical_verified"]) for r in canon)) /
                                                               n_calls["E1"], 3) if n_calls["E1"] else None},
        "note": "canonical claims are system-derived from telemetry provenance; never counted as model-written claims"}
    (OUT / "claim_metrics.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4b.claim_metrics/v1", **meta,
         "definitions": {
             "verified": "observed claim that passes the frozen verifier V0-V5 (verifier acceptance, not factual truth)",
             "retained_after_filtering": "verified observed claims / all generated claims (observed + inferred)",
             "about_true_root": "claim subject is a ground-truth primary root service (evaluation side)",
             "off_target": "claim subject is neither a root nor a ground-truth propagated-symptom service",
             "citations_all_known": "every cited alias exists in the prompt's record table",
             "valid_ids_but_rejected": "all citations resolve to records of the view, yet the verifier rejects the claim",
             "E0": "frozen E0 reference on the alert window (deterministic templates, no LLM)",
             "B4-G": "B4's stored claims on views where the frozen detector alerts"},
         "conditions": cm, "e2_ownership_rows": own_rows,
         "claims": {k: v for k, v in claims.items()}, "edges": edges}, indent=1, default=str) + "\n")

    # ---- condition comparison
    diag = {c: {"all": _diag_summary(ts), "by_spec": {s: _diag_summary(by_spec(ts, s)) for s, _ in SPECS}}
            for c, ts in trials.items()}
    flt = [r for r in rows.values() if r["fault_present"]]
    ctl = [r for r in rows.values() if not r["fault_present"]]
    silent_fault = [r for r in flt if not r["alert_fired"]]
    gate = {"fault_views_alerted": _rate(len(flt) - len(silent_fault), len(flt)),
            "missed_fault_views": _rate(len(silent_fault), len(flt)),
            "missed_fault_base_cases": sorted({r["scenario_key"] for r in silent_fault}),
            "control_views_alerted": _rate(sum(r["alert_fired"] for r in ctl), len(ctl)),
            "missed_by_spec": {s: sum(r["spec"] == s for r in silent_fault) for s, _ in SPECS},
            "counterfactual_call_saving": {
                "b4_calls_ungated": n_calls["B4"], "b4g_calls": n_calls["B4-G"],
                "calls_avoided": n_calls["B4"] - n_calls["B4-G"],
                "calls_avoided_on_controls": sum(not r["alert_fired"] for r in ctl),
                "calls_avoided_on_fault_views": len(silent_fault),
                "b4_correct_top1_lost_by_gate": sum(bool(t["top1_correct"]) for t in trials["B4"]
                                                    if t["fault_present"] and not t["alert_fired"]),
                "b4_false_diagnoses_removed_on_controls": sum(bool(t["false_diagnosis_on_control"]) for t in trials["B4"]
                                                              if not t["alert_fired"]),
                "llm_seconds_avoided": round(sum(t["wall_s"] or 0 for t in trials["B4"] if not t["alert_fired"]), 1),
                "note": "counterfactual: B4's stored calls on gate-silent views were made (B4 is ungated) and are "
                        "counted as calls B4-G would not make; no new call was run for B4-G"}}
    paired = {"E1_vs_B4_top1": _paired(trials["E1"], trials["B4"]),
              "E1_frozen_gated_vs_B4_top1": _paired(trials["E1-frozen-gated"], trials["B4"]),
              "B4G_vs_B4_top1": _paired(trials["B4-G"], trials["B4"]),
              "E1_vs_B4_fault_type": _paired(trials["E1"], trials["B4"], "fault_type_correct")}
    paired["claims_E1_vs_B4"] = _paired_claims(claims["E1"], claims["B4"])     # added after the run (disclosed)
    agree = [t for t in trials["E1"] if t["called"] and t["fault_present"]]
    llm_view = {"E1_llm_agrees_with_e0_top1": _rate(sum(t["llm_agrees_with_e0"] is True for t in agree), len(agree)),
                "E1_llm_own_suspect_correct": _rate(sum(bool(t["llm_suspect_correct"]) for t in agree), len(agree)),
                "E1_llm_own_ABSTAIN_on_fault_views": _rate(sum(t["llm_decision"] == "ABSTAIN" for t in agree), len(agree)),
                "E1_llm_own_ABSTAIN_on_controls": _rate(
                    sum(t["llm_decision"] == "ABSTAIN" for t in trials["E1"] if t["called"] and not t["fault_present"]),
                    sum(t["called"] and not t["fault_present"] for t in trials["E1"])),
                "B4_own_ABSTAIN_on_fault_views": _rate(
                    sum(t["llm_decision"] == "ABSTAIN" for t in trials["B4"] if t["called"] and t["fault_present"]),
                    sum(t["called"] and t["fault_present"] for t in trials["B4"])),
                "B4_own_ABSTAIN_on_controls": _rate(
                    sum(t["llm_decision"] == "ABSTAIN" for t in trials["B4"] if t["called"] and not t["fault_present"]),
                    sum(t["called"] and not t["fault_present"] for t in trials["B4"]))}
    frozen_ref = json.loads(Path("results/final/test_results.json").read_text())
    (OUT / "condition_comparison.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4b.condition_comparison/v1", **meta,
         "definitions": {
             "B4": "ungated LLM-only (p4a-v1): window = alert window, else median-time window; decision is the model's",
             "E1": "frozen E0 ranking/localization + p4a-v1 claims; same window rule as B4; diagnosis-level outcome is "
                   "E0's on that window (E0 receives no LLM input; E1's LLM sees E0's ranking, B4's does not)",
             "E1-frozen-gated": "E1 under the frozen protocol: no alert -> ABSTAIN (equals frozen E0 test outputs)",
             "E2": "E1 + deterministic ownership; diagnosis identical to E1 by construction",
             "B4-G": "hybrid: frozen detector gate, then B4's stored output; not an LLM-only baseline",
             "E0": "frozen deterministic reference on the alert window (already reported in docs/final_results.md)",
             "unit": "the base case (scenario) is the independent unit; views are repeated measures",
             "top1": "DIAGNOSE with the true root as suspected service, over all fault views"},
         "diagnosis": diag, "paired": paired, "llm_own": llm_view, "b4g_gate": gate,
         "abstention": {"B4": "own DIAGNOSE/ABSTAIN only: a single operating point; no confidence score, no RC curve",
                        "E0_frozen": {k: frozen_ref.get(k) for k in ("abstention",) if k in frozen_ref} or
                        "see results/final/test_results.json (S and A-margin, unchanged)"},
         "e0_reproduces_frozen_test_outputs": {"matched": sum(x is True for x in e0_repro),
                                              "checked": sum(x is not None for x in e0_repro)}},
        indent=1, default=str) + "\n")

    # ---- runtime
    walls = sorted(c["wall_s"] for c in calls if c["wall_s"] is not None)
    q = lambda p: walls[min(len(walls) - 1, int(p * len(walls)))] if walls else None
    rt = {}
    for cond in ("B4", "E1"):
        cs = [r["call_record"] for r in recs.values() if r["condition"] == cond]
        w = sorted(c["wall_s"] for c in cs if c["wall_s"] is not None)
        rt[cond] = {"calls": len(cs), "outcomes": dict(Counter(c["outcome"] for c in cs)),
                    "parse": dict(Counter((r["output"].get("parse") or {}).get("status") for r in recs.values()
                                          if r["condition"] == cond)),
                    "truncated_at_token_limit": sum(bool(c.get("truncated_at_token_limit")) for c in cs),
                    "wall_s": {"mean": round(statistics.mean(w), 2) if w else None,
                               "p50": w[len(w) // 2] if w else None, "p95": w[min(len(w) - 1, int(0.95 * len(w)))] if w else None,
                               "max": w[-1] if w else None, "total_h": round(sum(w) / 3600, 2)},
                    "prompt_tokens": {"mean": round(statistics.mean([c["prompt_tokens"] for c in cs if c["prompt_tokens"]]), 1)
                                      if cs else None, "max": max((c["prompt_tokens"] or 0 for c in cs), default=None),
                                      "total": sum(c["prompt_tokens"] or 0 for c in cs)},
                    "output_tokens": {"mean": round(statistics.mean([c["output_tokens"] for c in cs if c["output_tokens"]]), 1)
                                      if cs else None, "max": max((c["output_tokens"] or 0 for c in cs), default=None),
                                      "total": sum(c["output_tokens"] or 0 for c in cs)},
                    "output_tok_per_s_mean": round(statistics.mean([c["output_tokens_per_s"] for c in cs
                                                                    if c.get("output_tokens_per_s")]), 2) if cs else None,
                    "load_s_max": max((c["load_s"] or 0 for c in cs), default=None)}
    logs = sorted((OUT / "run_logs").glob("environment_before_*.json"))
    starts = [c["started_at"] for c in calls if c.get("started_at")]
    (OUT / "runtime_summary.json").write_text(json.dumps(
        {"schema": "evidencerca.phase4b.runtime_summary/v1", **meta, "per_condition": rt,
         "all_calls_wall_s": {"p50": q(0.5), "p95": q(0.95), "total_h": round(sum(walls) / 3600, 2)},
         "elapsed_first_to_last_call_h": round((max(starts) - min(starts)) / 3600, 2) if starts else None,
         "ollama_rss_peak_mb": max((r["ollama_rss_peak_mb"] or 0 for r in recs.values()), default=None),
         "transport_failures_retried": sum(len(r.get("earlier_transport_failures") or []) for r in recs.values()),
         "environment_snapshots": [l.name for l in logs], "api_cost_usd": 0.0,
         "raw_dir_mb": round(sum(p.stat().st_size for p in RAW.rglob("*.json")) / 2**20, 1) if RAW.exists() else 0},
        indent=1, default=str) + "\n")
    print(json.dumps({c: {"top1": d["all"]["top1"]["k"], "fault_views": d["all"]["fault_views"],
                          "ctrl_fd": d["all"]["control_false_diagnoses"]["k"], "calls": d["all"]["calls"]}
                      for c, d in diag.items()}, indent=1))
    return 0


# ---------------------------------------------------------------- blinded audit package (protocol 9.1)
def _verdict(c):
    """Verifier-accepted: observed -> verified; inferred -> premises valid (V1-V3). Sealed key only."""
    return bool(c["verified"]) if c["kind"] == "observed" else bool(c["premises_valid"])


def _pick_capped(pool, n, rng, used):
    """n claims: at least GROUP_MIN per predicate group when available, at most AUDIT_MAX_PER_CASE per base case
    (counted over the condition's whole sample via `used`), the rest at random."""
    pool = sorted(pool, key=lambda c: (str(c["k"]), c["claim_id"]))
    rng.shuffle(pool)
    out = []

    def take(c):
        if used[c["case_id"]] < AUDIT_MAX_PER_CASE and c not in out:
            out.append(c)
            used[c["case_id"]] += 1
            return True
        return False
    for g in sorted({pilot_v2._group(c) for c in pool}):
        got = 0
        for c in pool:
            if len(out) >= n or got >= pilot_v2.GROUP_MIN:
                break
            if pilot_v2._group(c) == g and take(c):
                got += 1
    for c in pool:
        if len(out) >= n:
            break
        take(c)
    return out


def _render_claim(c):
    return (f"{c['kind'].upper()}: {c['predicate']}(subject={c['subject_service']}"
            + (f", object={c['object_service']}" if c.get("object_service") else "") + ")"
            + (f" = {c['value']} {c['unit']}" if c["kind"] == "observed" else " [causal hypothesis; judge premises only]"))


def audit() -> int:
    plan = json.loads(PLAN.read_text())
    cm = json.loads((OUT / "claim_metrics.json").read_text())
    rows = {r["index"]: r for r in plan["views"]}
    gts = {c["case_id"]: c["gt"] for c in test_cases()}
    rng = random.Random(AUDIT_SEED)
    chosen, fill = [], {}
    for cond in ("E0", "E1", "B4"):
        pool = cm["claims"][cond]
        acc = [c for c in pool if _verdict(c)]
        rej = [c for c in pool if not _verdict(c)]
        half = AUDIT_PER_CONDITION // 2
        used = Counter()
        nv = min(len(acc), max(half, AUDIT_PER_CONDITION - len(rej)))
        a = _pick_capped(acc, nv, rng, used)
        r = _pick_capped(rej, AUDIT_PER_CONDITION - len(a), rng, used)
        if len(a) + len(r) < AUDIT_PER_CONDITION:          # rejected stratum short: fill from accepted
            a += _pick_capped([c for c in acc if c not in a], AUDIT_PER_CONDITION - len(a) - len(r), rng, used)
        fill[cond] = {"population": len(pool), "accepted_available": len(acc), "rejected_available": len(rej),
                      "taken_accepted": len(a), "taken_rejected": len(r),
                      "shortfall_note": None if len(r) >= half and len(a) >= half else
                      "one stratum had fewer eligible claims than 40 (after the per-base-case cap); filled from the other"}
        chosen += [dict(x, _cond=cond) for x in a + r]
    rng.shuffle(chosen)
    for i, c in enumerate(chosen):
        c["_aid"] = f"P{i + 1:03d}"
    recs = _load_records(plan)
    rec_by = {(r["view"]["index"], r["condition"]): r for r in recs.values()}
    items, keyrows = {}, {}
    cur = None
    for c in sorted(chosen, key=lambda c: (c["view_index"], c["_aid"])):
        row = rows[c["view_index"]]
        gt = gts[row["case_id"]]
        if cur is None or cur[0] != c["view_index"]:
            view, win = _load(row, gt)
            if c["_cond"] == "E0":
                win = detect_alert(view)
            cur = (c["view_index"], view, win)
        _, view, win = cur
        if c["_cond"] == "E0":                 # E0 always uses the alert window; LLM conditions the window rule
            win = detect_alert(view)
        else:
            win, _ = window_for(view)
        ids = c["evidence_ids"]
        cited = "\n".join(render_record(view.by_id[e], win.alert_ns).split(" | ", 1)[1] if e in view.by_id
                          else f"{e}: NOT IN THE TELEMETRY VIEW" for e in ids) or "(no citation)"
        ctx = pilot_v2._context(view, win, c, random.Random(f"{AUDIT_SEED}|{c['_aid']}"))
        g = observed_graph(view)
        edges_txt = "\n".join(f"{a} -> {b}" for a, b in sorted(g)) if g else "(no trace-derived call edges)"
        a0, a1 = win.analysis
        b0, _ = win.baseline
        hdr = (f"baseline [{(b0 - win.alert_ns) / 1e9:.0f}s, {(a0 - win.alert_ns) / 1e9:.0f}s) and analysis "
               f"[{(a0 - win.alert_ns) / 1e9:.0f}s, {(a1 - win.alert_ns) / 1e9:.0f}s) relative to the window anchor; "
               f"unavailable modalities: {', '.join(sorted(view.unavailable)) or 'none'}")
        aid = c["_aid"]
        items[aid] = {"audit_id": aid, "context_id": _sha(f"{AUDIT_SEED}|{row['view_id']}|{c['_cond'] == 'E0'}")[:10],
                      "windows": hdr, "claim": _render_claim(c), "cited_records": cited,
                      "context_baseline_same_family": ctx["baseline"], "context_analysis_same_family": ctx["analysis"],
                      "observed_call_edges": edges_txt,
                      "support_label": "", "partial_reason": "", "evidence_relevance": "", "notes": ""}
        keyrows[aid] = {"audit_id": aid, "condition": c["_cond"], "view_index": c["view_index"], "spec": row["spec"],
                        "scenario": row["scenario_key"], "case_id": row["case_id"], "claim_id": c["claim_id"],
                        "k": c["k"], "kind": c["kind"], "predicate_group": pilot_v2._group(c),
                        "verifier_accepted": _verdict(c), "verifier_checks": c.get("verifier_checks"),
                        "subject_is_true_root": c["subject_is_root"], "subject_in_symptoms": c["subject_in_symptoms"],
                        "ground_truth_roots": gt["primary_root_services"], "fault_type": gt["fault_type"]}
    fields = ["audit_id", "context_id", "windows", "claim", "cited_records", "context_baseline_same_family",
              "context_analysis_same_family", "observed_call_edges", "support_label", "partial_reason",
              "evidence_relevance", "notes"]
    with (OUT / "audit_blinded.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(items[a] for a in sorted(items))
    key = [keyrows[a] for a in sorted(keyrows)]
    groups = Counter((k["condition"], k["predicate_group"], k["verifier_accepted"]) for k in key)
    (OUT / "audit_key_sealed.json").write_text(json.dumps(
        {"warning": "SEALED: do not open before both annotators have submitted labels and the adjudication is done",
         "seed": AUDIT_SEED, "protocol": "docs/llm_evaluation_protocol.md section 9.1", "rows": key}, indent=1) + "\n")
    (OUT / "audit_manifest.json").write_text(json.dumps(
        {"n_items": len(items), "per_condition": dict(Counter(k["condition"] for k in key)), "stratum_fill": fill,
         "per_condition_group_verdict": {f"{a}|{b}|{'accepted' if c else 'rejected'}": n
                                         for (a, b, c), n in sorted(groups.items())},
         "max_per_base_case_and_condition": max(Counter((k["condition"], k["case_id"]) for k in key).values()),
         "csv_sha256": hashlib.sha256((OUT / "audit_blinded.csv").read_bytes()).hexdigest(),
         "key_sha256": hashlib.sha256((OUT / "audit_key_sealed.json").read_bytes()).hexdigest(),
         "status": "PENDING: no human labels exist; the verifier's verdicts are not labels",
         "blinded_fields_removed": ["condition", "prompt version", "model wording and explanation", "selection tags",
                                    "verifier verdict and statistics", "ground truth and fault labels",
                                    "case, view and scenario ids"]}, indent=1) + "\n")
    print(json.dumps(fill, indent=1))
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan":
        verify_frozen()
        OUT.mkdir(parents=True, exist_ok=True)
        plan = make_plan()
        PLAN.write_text(json.dumps(plan, indent=1) + "\n")
        print({k: plan[k] for k in ("n_cases", "n_fault_cases", "n_control_cases", "n_views", "n_calls",
                                    "n_calls_by_condition", "views_alerted", "e0_reproduces_frozen_on_alerted_views",
                                    "equal_record_budget_B4_E1")}, plan["leakage"]["passed"])
        return 0
    return {"run": run, "repro": repro, "analyze": analyze, "audit": audit}.get(cmd, lambda: print(__doc__) or 2)()


if __name__ == "__main__":
    sys.exit(main())
