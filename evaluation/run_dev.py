"""Phase 3 development evaluation (dev seed 11 only). Evaluation side: joins ground truth here and only here.

  uv run python -m evaluation.run_dev            # -> results/phase3/dev_results.json, abstention_dev_results.json

Never run while a capture is in progress (host contention invalidates captures; see docs/phase3_validation.md).
"""

import argparse
import hashlib
import json
import resource
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evaluation.views import SPECS, build, oracle_anchor
from evidencerca import paths, rca_input
from evidencerca.abstention import AbstentionConfig, score
from evidencerca.core import detect_alert, make_windows
from evidencerca.evidence import normalize
from evidencerca.methods.baselines import b1, b2, b3
from evidencerca.methods.evidencerca_e0 import diagnose_full
from evidencerca.verifier import verify
from harness.schedule import SPLITS

OUT = Path("results/phase3")
METHODS = ("B1", "B2", "B3", "E0", "E0-A-graph")
TAU_GRID = [round(x * 0.05, 2) for x in range(0, 21)]


def dev_cases() -> list[dict]:
    """Valid protocol-timeline dev cases: pilot run A, the dev matrix and the CPU-variant cases.
    Pilot run B is a replicate of A; it is evaluated separately for method-output repeatability."""
    out, seen = [], set()
    for line in (paths.DATA_ROOT / "harness_index.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["group"] not in ("pilot", "matrix", "cpu_variant"):
            continue
        gt = json.loads((paths.GROUND_TRUTH_ROOT / e["case_id"] / "ground_truth.json").read_text())
        seed = gt["scenario"]["master_seed"]
        if SPLITS.get(seed) != "dev":
            raise PermissionError("non-dev case in the index; Phase 3 uses dev seed 11 only")
        if not gt["case_valid"]:
            continue
        role = "replicate" if e["group"] == "pilot" and e["label"] == "B" else "primary"
        key = gt["scenario"]["key"]
        if role == "primary" and key in seen:
            continue
        seen.add(key) if role == "primary" else None
        out.append({"case_id": e["case_id"], "role": role, "gt": gt})
    return out


def run_method(m, view, win):
    t = time.perf_counter()
    signals, verification = None, None
    if m == "B1":
        d = b1(view, win)
    elif m == "B2":
        d = b2(view, win)
    elif m == "B3":
        d = b3(view, win)
    else:
        d, signals = diagnose_full(view, win, use_graph=(m == "E0"))
    t_rank = time.perf_counter() - t
    t_ver = 0.0
    if m.startswith("E0") and d["decision"] == "DIAGNOSE":
        t0 = time.perf_counter()
        verification = verify(d, view)
        t_ver = time.perf_counter() - t0
    ab = score(d, signals if m.startswith("E0") else None, verification if m.startswith("E0") else None,
               view.unavailable, AbstentionConfig())
    return d, verification, ab, t_rank, t_ver


def trial_record(case, spec, seed, anchor, win, m, d, ver, ab, t_rank, t_ver):
    gt = case["gt"]
    roots = gt["primary_root_services"]
    ranked = [c["service"] for c in d["ranked_candidates"] if c["score"] > 0]
    rec = {"case_id": case["case_id"], "role": case["role"], "scenario": gt["scenario"]["key"],
           "fault_type": gt["fault_type"],
           "variant": gt["params"].get("variant", "standard"), "condition": spec, "perturbation_seed": seed,
           "anchor": anchor, "triggered": win.triggered, "method": m, "decision": d["decision"],
           "suspected_service": d["suspected_service"], "top3": ranked[:3],
           "suspected_fault_type": d["suspected_fault_type"],
           "top1_correct": (d["suspected_service"] in roots) if roots else None,
           "top3_correct": (any(s in roots for s in ranked[:3])) if roots else None,
           "fault_type_correct": (d["suspected_fault_type"] == gt["fault_type"]) if roots and d["suspected_fault_type"]
           else None,
           "S": ab["S"], "S_margin": ab["S_margin"], "abstention_components": ab.get("components"),
           "runtime_rank_s": round(t_rank, 4), "runtime_verify_s": round(t_ver, 4),
           "output_sha256": hashlib.sha256(json.dumps({k: v for k, v in d.items() if k != "_internal"},
                                                      sort_keys=True, default=str).encode()).hexdigest()[:16]}
    if ver is not None:
        rec["verification"] = {**ver["summary"], "rejected_claim_ids": ver["rejected_claim_ids"], "flags": ver["flags"],
                               "rejected_checks": [{"predicate": c["predicate"],
                                                    "failed": [k for k, v in c["checks"].items() if v == "fail"]}
                                                   for c in ver["claims"] if c["kind"] == "observed" and not c["verified"]]}
        rec["claims"] = [{"predicate": c["predicate"], "subject": c["subject_service"], "kind": c["kind"],
                          "verified": c["verified"]} for c in ver["claims"]]
    return rec


def rc_curve(items):
    """items: (score, error) for diagnosed trials. Returns AURC and the curve (coverage, risk)."""
    if not items:
        return None, []
    items = sorted(items, key=lambda x: -x[0])
    curve, errs = [], 0
    for k, (_, e) in enumerate(items, 1):
        errs += e
        curve.append((round(k / len(items), 4), round(errs / k, 4)))
    return round(statistics.mean(r for _, r in curve), 4), curve


def aggregate(trials):
    groups = defaultdict(list)
    for t in trials:
        if t["role"] != "primary":
            continue
        groups[(t["method"], t["anchor"], t["condition"])].append(t)   # R* pool their 3 perturbation seeds
    out = []
    for (m, anchor, cond), ts in sorted(groups.items()):
        faults = [t for t in ts if t["fault_type"] != "none"]
        trig = [t for t in faults if t["triggered"]]
        diag = [t for t in trig if t["decision"] == "DIAGNOSE"]
        ctrl = [t for t in ts if t["fault_type"] == "none"]
        row = {"method": m, "anchor": anchor, "condition": cond,
               "n_fault_trials": len(faults), "n_triggered": len(trig), "detection_failures": len(faults) - len(trig),
               "n_diagnosed": len(diag),
               "top1_correct": sum(t["top1_correct"] for t in trig), "top3_correct": sum(t["top3_correct"] for t in trig),
               "top1_rate_of_triggered": round(sum(t["top1_correct"] for t in trig) / len(trig), 4) if trig else None,
               "top3_rate_of_triggered": round(sum(t["top3_correct"] for t in trig) / len(trig), 4) if trig else None,
               "top1_rate_of_all_fault_trials": round(sum(t["top1_correct"] for t in trig) / len(faults), 4) if faults else None,
               "n_control_trials": len(ctrl), "control_alerts": sum(t["triggered"] for t in ctrl),
               "control_diagnosed": sum(t["decision"] == "DIAGNOSE" for t in ctrl)}
        if m.startswith("E0"):
            ft = [t for t in diag if t["fault_type_correct"] is not None]
            row["fault_type_correct"] = sum(t["fault_type_correct"] for t in ft)
            row["fault_type_n"] = len(ft)
        out.append(row)
    return out


def main() -> int:
    argparse.ArgumentParser().parse_args()
    cases = dev_cases()
    trials, transform_logs, norm_times = [], [], []
    t_all = time.time()
    for ci, case in enumerate(cases):
        t0 = time.perf_counter()
        clean = normalize(rca_input.load_case(case["case_id"]))
        norm_times.append(time.perf_counter() - t0)
        specs = SPECS if case["role"] == "primary" else [("C0", 1)]
        for spec, seed in specs:
            view, tlog = build(clean, case["case_id"], spec, seed, case["gt"])
            transform_logs.append({"case_id": case["case_id"], "view_id": view.view_id, **tlog})
            windows = {"alert": detect_alert(view),
                       "oracle": make_windows(view, oracle_anchor(case["gt"]), "oracle")}
            for anchor, win in windows.items():
                for m in METHODS:
                    d, ver, ab, tr, tv = run_method(m, view, win)
                    trials.append(trial_record(case, spec, seed, anchor, win, m, d, ver, ab, tr, tv))
        print(f"[{ci + 1}/{len(cases)}] {case['gt']['scenario']['key']} ({case['role']}) done", flush=True)
    wall = time.time() - t_all
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)  # macOS reports bytes
    runtime = {}
    for m in METHODS:
        r = sorted(t["runtime_rank_s"] for t in trials if t["method"] == m)
        v = sorted(t["runtime_verify_s"] for t in trials if t["method"] == m and t.get("verification"))
        runtime[m] = {"n": len(r), "rank_p50_s": r[len(r) // 2], "rank_p95_s": r[int(0.95 * len(r))],
                      "verify_p50_s": v[len(v) // 2] if v else None, "verify_p95_s": v[int(0.95 * len(v))] if v else None}
    summary_cases = [{"case_id": c["case_id"], "role": c["role"], "scenario": c["gt"]["scenario"]["key"],
                      "fault_type": c["gt"]["fault_type"], "target": c["gt"]["target"],
                      "variant": c["gt"]["params"].get("variant", "standard"),
                      "observable_impact": c["gt"]["observable_impact"]["gateway_slo_deviated"]} for c in cases]
    OUT.mkdir(parents=True, exist_ok=True)
    dev = {"schema": "evidencerca.phase3.dev_results/v1",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "split": "dev (seed 11) only", "cases": summary_cases,
           "n_primary_cases": sum(c["role"] == "primary" for c in cases),
           "views_per_primary_case": len(SPECS), "methods": METHODS, "anchors": ["alert", "oracle"],
           "aggregates": aggregate(trials),
           "replicate_consistency": replicate_consistency(trials),
           "runtime": {"per_method": runtime, "normalize_per_case_s": {"p50": statistics.median(norm_times),
                                                                        "max": max(norm_times)},
                       "total_wall_s": round(wall, 1), "peak_rss_mb": round(rss_mb, 1),
                       "note": "evaluation process only; capture is not running concurrently"},
           "transform_logs": transform_logs, "trials": trials}
    (OUT / "dev_results.json").write_text(json.dumps(dev, indent=1, default=str) + "\n")
    (OUT / "abstention_dev_results.json").write_text(json.dumps(abstention_report(trials), indent=1) + "\n")
    print(f"done: {len(trials)} trials in {wall:.0f} s, peak RSS {rss_mb:.0f} MB")
    return 0


def replicate_consistency(trials):
    """Pilot run B (replicate captures) vs run A on the clean view: same outputs?"""
    a = {(t["scenario"], t["method"], t["anchor"]): t for t in trials if t["role"] == "primary"
         and t["condition"] == "C0"}
    out = []
    for t in trials:
        if t["role"] != "replicate":
            continue
        ref = a.get((t["scenario"], t["method"], t["anchor"]))
        if ref and ref["case_id"] != t["case_id"]:
            out.append({"scenario": t["scenario"], "method": t["method"], "anchor": t["anchor"],
                        "same_suspected_service": ref["suspected_service"] == t["suspected_service"],
                        "same_decision": ref["decision"] == t["decision"]})
    return out


def abstention_report(trials):
    rep = {"schema": "evidencerca.phase3.abstention_dev/v1", "split": "dev (seed 11) only",
           "note": ("S and S_margin are heuristic ordering scores, not probabilities. tau is NOT calibrated here: "
                    "per protocol it is chosen on calibration seeds only. Dev curves are descriptive."),
           "weights": AbstentionConfig().weights, "by_method": {}}
    prim = [t for t in trials if t["role"] == "primary" and t["anchor"] == "alert"]
    for m in METHODS:
        mt = [t for t in prim if t["method"] == m]
        faults = [t for t in mt if t["fault_type"] != "none" and t["decision"] == "DIAGNOSE"]
        ctrl = [t for t in mt if t["fault_type"] == "none"]
        entry = {}
        for sname in (("S", "S_margin") if m.startswith("E0") else ("S_margin",)):
            aurc, curve = rc_curve([(t[sname], 0 if t["top1_correct"] else 1) for t in faults])
            grid = []
            for tau in TAU_GRID:
                kept = [t for t in faults if t[sname] >= tau]
                grid.append({"tau": tau, "coverage": round(len(kept) / len(faults), 4) if faults else None,
                             "selective_risk": round(sum(not t["top1_correct"] for t in kept) / len(kept), 4)
                             if kept else None, "n_kept": len(kept),
                             "controls_diagnosed": sum(t["decision"] == "DIAGNOSE" and t[sname] >= tau for t in ctrl)})
            by_cond = {}
            for cond in sorted({t["condition"] for t in faults}):
                ft = [t for t in faults if t["condition"] == cond]
                by_cond[cond] = {"n": len(ft), "aurc": rc_curve([(t[sname], 0 if t["top1_correct"] else 1)
                                                                 for t in ft])[0]}
            entry["A-margin" if sname == "S_margin" and m.startswith("E0") else sname] = {
                "n_diagnosed_fault_trials": len(faults), "aurc": aurc, "tau_grid": grid, "aurc_by_condition": by_cond,
                "rc_curve": curve}
        entry["controls"] = {"n_trials": len(ctrl), "alerts": sum(t["triggered"] for t in ctrl),
                             "diagnosed": sum(t["decision"] == "DIAGNOSE" for t in ctrl)}
        rep["by_method"][m] = entry
    return rep


if __name__ == "__main__":
    sys.exit(main())
