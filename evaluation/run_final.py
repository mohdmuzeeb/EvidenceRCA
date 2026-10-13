"""Frozen calibration and held-out test evaluation of the deterministic methods (protocol §§7-9).

  uv run python -m evaluation.run_final calibrate   # seeds 23/37 only -> results/final/calibration.json
  uv run python -m evaluation.run_final evaluate    # seeds 41/53/67 with the frozen tau -> results/final/test_*.json

Both stages verify the freeze manifest first. `calibrate` never reads test labels; `evaluate` refuses to run without
the calibration file and records its hash. Nothing here may change after test results are seen.
"""

import hashlib
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evaluation.freeze import SPLITS, verify_frozen
from evaluation.run_dev import METHODS, run_method, trial_record
from evaluation.views import SPECS, build, oracle_anchor
from evidencerca import paths, rca_input
from evidencerca.core import detect_alert, make_windows
from evidencerca.evidence import normalize

OUT = Path("results/final")
CAL_FILE = OUT / "calibration.json"
B = 10_000
SCORES = {  # name -> (method, how to read the score from a trial)
    "E0:S": ("E0", lambda t: t["S"]),
    "E0:A-margin": ("E0", lambda t: t["S_margin"]),
    "E0:A-ver": ("E0", lambda t: _no_coverage(t)),
    "E0-A-graph:S": ("E0-A-graph", lambda t: t["S"]),
    "B1:margin": ("B1", lambda t: t["S_margin"]),
    "B2:margin": ("B2", lambda t: t["S_margin"]),
    "B3:margin": ("B3", lambda t: t["S_margin"]),
}


def _no_coverage(t):
    c = t.get("abstention_components") or {}
    xs = [c.get(k) for k in ("sufficiency", "agreement", "margin") if c.get(k) is not None]
    return sum(xs) / len(xs) if xs else 0.0


# ---------------------------------------------------------------- data
def cases_for(split: str) -> list[dict]:
    seeds = set(SPLITS[split])
    out = []
    for line in (paths.DATA_ROOT / "harness_index.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["group"] != split:
            continue
        gt = json.loads((paths.GROUND_TRUTH_ROOT / e["case_id"] / "ground_truth.json").read_text())
        if gt["scenario"]["master_seed"] not in seeds:
            raise PermissionError(f"case {e['case_id']} of split {split} has seed {gt['scenario']['master_seed']}")
        if gt["case_valid"]:
            out.append({"case_id": e["case_id"], "role": "primary", "gt": gt})
    keys = [c["gt"]["scenario"]["key"] for c in out]
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"duplicate valid captures in split {split}")
    return out


def run_trials(cases) -> list[dict]:
    trials = []
    for i, case in enumerate(cases):
        clean = normalize(rca_input.load_case(case["case_id"]))
        for spec, seed in SPECS:
            view, _ = build(clean, case["case_id"], spec, seed, case["gt"])
            for anchor, win in (("alert", detect_alert(view)),
                                ("oracle", make_windows(view, oracle_anchor(case["gt"]), "oracle"))):
                for m in METHODS:
                    d, ver, ab, tr, tv = run_method(m, view, win)
                    t = trial_record(case, spec, seed, anchor, win, m, d, ver, ab, tr, tv)
                    t["seed"] = case["gt"]["scenario"]["master_seed"]
                    trials.append(t)
        print(f"[{i + 1}/{len(cases)}] {case['gt']['scenario']['key']}", flush=True)
    return trials


# ---------------------------------------------------------------- statistics
def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def cluster_boot(per_case: dict, stat=statistics.mean, seed=7):
    """95 % percentile CI of `stat` over base cases resampled with replacement."""
    keys = sorted(per_case)
    if len(keys) < 2:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(B):
        sample = [per_case[keys[rng.randrange(len(keys))]] for _ in keys]
        vals.append(stat(sample))
    vals.sort()
    return [round(vals[int(0.025 * B)], 4), round(vals[int(0.975 * B) - 1], 4)]


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, p)


def holm(pvals: dict) -> dict:
    items = sorted(pvals.items(), key=lambda x: x[1])
    m, out, running = len(items), {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = round(running, 6)
    return out


def rc(items):
    """items: (score, error 0/1, case_id). AURC over the diagnosed trials (lower is better)."""
    if not items:
        return None, []
    items = sorted(items, key=lambda x: -x[0])
    curve, err = [], 0
    for k, it in enumerate(items, 1):
        err += it[1]
        curve.append(err / k)
    return statistics.mean(curve), curve


# ---------------------------------------------------------------- calibration
def choose_tau(items):
    """items: (score, error). op1: min selective risk s.t. coverage >= 0.7; op2: max coverage s.t. risk <= 0.1."""
    if not items:
        return {"op1": None, "op2": None}
    cands = sorted({round(s, 6) for s, _ in items})
    n = len(items)
    rows = []
    for tau in cands:
        kept = [e for s, e in items if s >= tau]
        rows.append((tau, len(kept) / n, sum(kept) / len(kept) if kept else None))
    op1 = [r for r in rows if r[1] >= 0.7 and r[2] is not None]
    op1 = min(op1, key=lambda r: (r[2], -r[1])) if op1 else None
    op2 = [r for r in rows if r[2] is not None and r[2] <= 0.1]
    op2 = max(op2, key=lambda r: (r[1], -r[0])) if op2 else None
    fmt = lambda r: None if r is None else {"tau": r[0], "coverage": round(r[1], 4), "selective_risk": round(r[2], 4)}
    return {"op1": fmt(op1), "op2": fmt(op2), "n_diagnosed_fault_trials": n}


def calibrate() -> int:
    man = verify_frozen()
    cases = cases_for("calibration")
    t0 = time.time()
    trials = run_trials(cases)
    taus = {}
    for name, (m, get) in SCORES.items():
        items = [(get(t), 0 if t["top1_correct"] else 1) for t in trials if t["method"] == m
                 and t["anchor"] == "alert" and t["fault_type"] != "none" and t["decision"] == "DIAGNOSE"]
        taus[name] = choose_tau(items)
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "calibration_trials.jsonl", "w") as f:
        for t in trials:
            f.write(json.dumps(t, default=str) + "\n")
    cal = {"schema": "evidencerca.final.calibration/v1", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "freeze_manifest_sha256": hashlib.sha256(Path("docs/frozen/freeze_manifest.json").read_bytes()).hexdigest(),
           "seeds": SPLITS["calibration"], "n_cases": len(cases), "n_trials": len(trials),
           "tau": taus, "runtime_s": round(time.time() - t0, 1),
           "rule": man["abstention"]["tau_rule"]}
    CAL_FILE.write_text(json.dumps(cal, indent=1) + "\n")
    print(json.dumps(taus, indent=1))
    return 0


# ---------------------------------------------------------------- evaluation
def per_case_acc(ts, field="top1_correct"):
    """Mean over perturbation seeds per base case (protocol §9), triggered trials count as their outcome,
    non-triggered fault trials count as incorrect (detection failure)."""
    pc = defaultdict(list)
    for t in ts:
        pc[t["case_id"]].append(1.0 if (t["triggered"] and t[field]) else 0.0)
    return {k: statistics.mean(v) for k, v in pc.items()}


def evaluate() -> int:
    verify_frozen()
    if not CAL_FILE.exists():
        raise RuntimeError("calibration must be completed (and frozen) before the test evaluation")
    cal = json.loads(CAL_FILE.read_text())
    cal_sha = hashlib.sha256(CAL_FILE.read_bytes()).hexdigest()
    cases = cases_for("test")
    t0 = time.time()
    trials = run_trials(cases)
    with open(OUT / "test_trials.jsonl", "w") as f:   # raw per-case predictions and abstention decisions
        for t in trials:
            for name, (m, get) in SCORES.items():
                if t["method"] == m:
                    for op in ("op1", "op2"):
                        tau = (cal["tau"][name].get(op) or {}).get("tau")
                        t.setdefault("abstention_decisions", {})[f"{name}@{op}"] = (
                            None if tau is None else ("DIAGNOSE" if t["decision"] == "DIAGNOSE" and get(t) >= tau
                                                      else "ABSTAIN"))
            f.write(json.dumps(t, default=str) + "\n")
    res = {"schema": "evidencerca.final.test_results/v1",
           "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "calibration_sha256": cal_sha, "seeds": SPLITS["test"], "n_cases": len(cases),
           "case_counts": _counts(cases), "n_trials": len(trials)}
    res["accuracy"] = accuracy_tables(trials)
    res["per_fault_type_C0"] = per_fault(trials)
    res["abstention"] = abstention_tables(trials, cal)
    res["verification"] = verification_tables(trials)
    res["paired_comparisons"] = paired(trials)
    res["runtime"] = runtime(trials, time.time() - t0)
    (OUT / "test_results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    print(json.dumps({k: res[k] for k in ("n_cases", "case_counts", "n_trials")}, indent=1))
    return 0


def _counts(cases):
    c = defaultdict(int)
    for x in cases:
        c[x["gt"]["fault_type"]] += 1
    return dict(c)


def accuracy_tables(trials):
    out = []
    groups = defaultdict(list)
    for t in trials:
        groups[(t["method"], t["anchor"], t["condition"])].append(t)
    for (m, anchor, cond), ts in sorted(groups.items()):
        faults = [t for t in ts if t["fault_type"] != "none"]
        ctrl = [t for t in ts if t["fault_type"] == "none"]
        trig = [t for t in faults if t["triggered"]]
        row = {"method": m, "anchor": anchor, "condition": cond, "n_base_cases": len({t["case_id"] for t in faults}),
               "n_fault_trials": len(faults), "n_triggered": len(trig),
               "detection_coverage": round(len(trig) / len(faults), 4) if faults else None,
               "detection_coverage_ci": wilson(len(trig), len(faults)),
               "control_trials": len(ctrl), "control_alerts": sum(t["triggered"] for t in ctrl),
               "control_diagnosed": sum(t["decision"] == "DIAGNOSE" for t in ctrl)}
        for f in ("top1_correct", "top3_correct"):
            k = sum(bool(t[f]) for t in trig)
            row[f"{f}_of_triggered"] = k
            row[f"{f}_rate_of_triggered"] = round(k / len(trig), 4) if trig else None
            pc = per_case_acc(faults, f)
            row[f"{f}_rate_all_faults"] = round(statistics.mean(pc.values()), 4) if pc else None
            row[f"{f}_rate_all_faults_ci"] = (wilson(sum(pc.values()), len(pc)) if not cond.startswith("R")
                                               else cluster_boot(pc))
        if m.startswith("E0"):
            ft = [t for t in trig if t["decision"] == "DIAGNOSE" and t["fault_type_correct"] is not None]
            row["fault_type_correct"], row["fault_type_n"] = sum(t["fault_type_correct"] for t in ft), len(ft)
        out.append(row)
    return out


def per_fault(trials):
    out = defaultdict(dict)
    for t in trials:
        if t["condition"] != "C0" or t["anchor"] != "alert" or t["fault_type"] == "none":
            continue
        d = out[t["fault_type"]].setdefault(t["method"], {"n": 0, "triggered": 0, "top1": 0, "top3": 0})
        d["n"] += 1
        d["triggered"] += t["triggered"]
        d["top1"] += bool(t["triggered"] and t["top1_correct"])
        d["top3"] += bool(t["triggered"] and t["top3_correct"])
    return out


def abstention_tables(trials, cal):
    out = {}
    for name, (m, get) in SCORES.items():
        ts = [t for t in trials if t["method"] == m and t["anchor"] == "alert"]
        diag = [t for t in ts if t["fault_type"] != "none" and t["decision"] == "DIAGNOSE"]
        items = [(get(t), 0 if t["top1_correct"] else 1, t["case_id"]) for t in diag]
        aurc, _ = rc(items)
        by_case = defaultdict(list)
        for it in items:
            by_case[it[2]].append(it)
        aurc_ci = cluster_boot(by_case, stat=lambda groups: rc([x for g in groups for x in g])[0]) if by_case else None
        entry = {"n_diagnosed_fault_trials": len(diag), "aurc": round(aurc, 4) if aurc is not None else None,
                 "aurc_ci": aurc_ci, "A-abst_selective_risk_at_full_coverage":
                     round(sum(e for _, e, _ in items) / len(items), 4) if items else None}
        ctrl = [t for t in ts if t["fault_type"] == "none"]
        for op in ("op1", "op2"):
            tau = (cal["tau"][name].get(op) or {}).get("tau")
            if tau is None:
                entry[op] = {"tau": None, "note": "not achievable on calibration seeds"}
                continue
            kept = [it for it in items if it[0] >= tau]
            entry[op] = {"tau": tau, "coverage": round(len(kept) / len(items), 4) if items else None,
                         "coverage_ci": wilson(len(kept), len(items)),
                         "selective_risk": round(sum(e for _, e, _ in kept) / len(kept), 4) if kept else None,
                         "selective_risk_ci": wilson(sum(e for _, e, _ in kept), len(kept)),
                         "controls_diagnosed": sum(t["decision"] == "DIAGNOSE" and get(t) >= tau for t in ctrl),
                         "control_trials": len(ctrl)}
        out[name] = entry
    return out


def verification_tables(trials):
    out = defaultdict(lambda: {"diagnoses": 0, "observed": 0, "verified": 0, "inferred": 0, "flagged": 0,
                               "edges": 0, "edges_verified": 0})
    rejected = defaultdict(int)
    for t in trials:
        if t["method"] != "E0" or t["anchor"] != "alert" or not t.get("verification"):
            continue
        v, o = t["verification"], out[t["condition"]]
        o["diagnoses"] += 1
        o["observed"] += v["observed"]
        o["verified"] += v["observed_verified"]
        o["inferred"] += v["inferred"]
        o["flagged"] += len(v["flags"])
        o["edges"] += v["edges"]
        o["edges_verified"] += v["edges_verified"]
        for r in v["rejected_checks"]:
            rejected[f"{r['predicate']}:{','.join(r['failed'])}"] += 1
    for o in out.values():
        o["verified_rate"] = round(o["verified"] / o["observed"], 4) if o["observed"] else None
        o["verified_per_diagnose"] = round(o["verified"] / o["diagnoses"], 3) if o["diagnoses"] else None
    return {"by_condition": dict(out), "rejections": dict(sorted(rejected.items(), key=lambda x: -x[1]))}


def paired(trials):
    """E0 vs each competitor on top-1 (detection failures count as wrong), per condition: cluster-bootstrap CI of the
    difference over base cases; exact McNemar at C0; Holm within each condition's family of 4 comparisons."""
    out = {}
    idx = defaultdict(dict)
    for t in trials:
        if t["anchor"] == "alert" and t["fault_type"] != "none":
            idx[(t["condition"], t["method"])][(t["case_id"], t["perturbation_seed"])] = bool(t["triggered"] and t["top1_correct"])
    for cond in sorted({c for c, _ in idx}):
        fam = {}
        for comp in ("B1", "B2", "B3", "E0-A-graph"):
            e, o = idx[(cond, "E0")], idx[(cond, comp)]
            keys = sorted(set(e) & set(o))
            per_case = defaultdict(list)
            for k in keys:
                per_case[k[0]].append(int(e[k]) - int(o[k]))
            diff = {c: statistics.mean(v) for c, v in per_case.items()}
            entry = {"n_base_cases": len(diff), "mean_diff_top1": round(statistics.mean(diff.values()), 4) if diff else None,
                     "ci_cluster_bootstrap": cluster_boot(diff)}
            if cond == "C0":
                b = sum(e[k] and not o[k] for k in keys)
                c = sum(o[k] and not e[k] for k in keys)
                entry.update(discordant_e0_only=b, discordant_comp_only=c, mcnemar_exact_p=round(mcnemar_exact(b, c), 6))
                fam[comp] = entry["mcnemar_exact_p"]
            out.setdefault(cond, {})[f"E0_vs_{comp}"] = entry
        if fam:
            for comp, p in holm(fam).items():
                out[cond][f"E0_vs_{comp}"]["holm_p"] = p
    return out


def runtime(trials, wall):
    r = {}
    for m in METHODS:
        x = sorted(t["runtime_rank_s"] for t in trials if t["method"] == m)
        v = sorted(t["runtime_verify_s"] for t in trials if t["method"] == m and t.get("verification"))
        r[m] = {"rank_p50_s": x[len(x) // 2], "rank_p95_s": x[int(0.95 * len(x))],
                "verify_p50_s": v[len(v) // 2] if v else None, "verify_p95_s": v[int(0.95 * len(v))] if v else None}
    return {"per_method": r, "total_wall_s": round(wall, 1)}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "calibrate":
        sys.exit(calibrate())
    if cmd == "evaluate":
        sys.exit(evaluate())
    raise SystemExit(__doc__)
