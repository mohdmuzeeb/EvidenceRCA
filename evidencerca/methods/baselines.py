"""Deterministic baselines B1-B3 (see docs/baseline_implementation.md for what is reproduced vs approximated).

All three take the same View and Windows as EvidenceRCA and emit the protocol's Diagnosis JSON
(ranked_candidates / suspected_service; no claims, so claim fields stay empty).
"""

from collections import defaultdict
from itertools import combinations

import numpy as np
from sklearn.cluster import Birch

from evidencerca.core import BUCKET_NS, Windows, observed_graph, robust
from evidencerca.evidence import ALL_SERVICES, APP_SERVICES, View
from evidencerca.methods.diagnosis import empty_diagnosis, finish


def _bucket_values(records, t0, t1, agg):
    b = defaultdict(list)
    for r in records:
        if t0 <= r["ts_start"] < t1:
            b[(r["ts_start"] - t0) // BUCKET_NS].append(r)
    return {k: agg(rs) for k, rs in b.items() if rs}


# ================================================================ B1: robust statistical ranking
# feature -> (record selector, per-bucket aggregate, absolute scale floor)
B1_FEATURES = {
    "latency":        (lambda v, s: v.select(modality="trace", service=s, kind="SERVER") if s != "postgres"
                       else v.select(modality="trace", kind="CLIENT", peer="postgres"),
                       lambda rs: float(np.median([r["value"] for r in rs])), 1.0),
    "error_rate":     (lambda v, s: v.select(modality="trace", service=s, kind="SERVER") if s != "postgres"
                       else v.select(modality="trace", kind="CLIENT", peer="postgres"),
                       lambda rs: sum(r["status"] == "error" for r in rs) / len(rs), 0.01),
    "container_cpu":  (lambda v, s: v.select(service=s, measurement="container_cpu"),
                       lambda rs: float(np.mean([r["value"] for r in rs])), 1.0),
    "container_memory": (lambda v, s: v.select(service=s, measurement="container_memory"),
                         lambda rs: float(np.mean([r["value"] for r in rs])), 1e6),
}
B1_MODALITY = {"latency": "trace", "error_rate": "trace", "container_cpu": "metric", "container_memory": "metric"}


def b1_scores(view: View, win: Windows) -> dict:
    """score(s) = max over features of the positive robust z of the analysis-window median vs the baseline."""
    scores = {}
    for s in ALL_SERVICES:
        zs = []
        for f, (sel, agg, floor) in B1_FEATURES.items():
            if B1_MODALITY[f] in view.unavailable:
                continue
            recs = sel(view, s)
            base = list(_bucket_values(recs, *win.baseline, agg).values())
            ana = list(_bucket_values(recs, *win.analysis, agg).values())
            if len(base) < 3 or not ana:
                continue
            med, sd = robust(base)
            zs.append(max(0.0, (float(np.median(ana)) - med) / max(sd, floor)))
        scores[s] = max(zs) if zs else 0.0
    return scores


def b1(view: View, win: Windows) -> dict:
    diag = empty_diagnosis(view, "B1", win)
    if not win.triggered:
        diag["uncertainty_reasons"].append("no alert: analysis window undefined")
        return diag
    return finish(diag, b1_scores(view, win))


# ================================================================ B2: MicroRCA-inspired
B2_BIRCH_THRESHOLD = 0.045   # seconds of response time (as published)
B2_ALPHA = 0.55              # weight of anomalous edges (as published)
B2_RESTART = 0.15            # personalized PageRank restart probability c


def _series(records, t0, t1, n, agg, fill=0.0):
    vals = _bucket_values(records, t0, t1, agg)
    out, last = [], fill
    for k in range(n):
        last = vals.get(k, last)
        out.append(last)
    return np.asarray(out, dtype=float)


def _corr(a, b) -> float:
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(abs(np.corrcoef(a, b)[0, 1]))


def microrca_ppv(nodes, call_edges, weights, preference, c=B2_RESTART, iters=500, tol=1e-12):
    """MicroRCA Eq. 4: v = (1 - c) P v + c u, with P_ij = w_ij / sum_j w_ij over the *reversed* edges
    (callee -> caller, host -> service). `call_edges` are in request direction (caller -> callee, service -> host);
    `weights` are keyed the same way. P is row-stochastic (or zero for rows without out-links), so the iteration
    contracts with rate (1 - c) and converges on any graph, including cycles and disconnected components."""
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    if n == 0:
        return {}
    u = np.array([preference.get(x, 0.0) for x in nodes], dtype=float)
    u = u / u.sum() if u.sum() > 0 else np.full(n, 1.0 / n)
    P = np.zeros((n, n))
    for (a, b) in call_edges:                       # reversed edge b -> a
        w = weights.get((a, b), 0.0)
        if w > 0 and a in idx and b in idx:
            P[idx[b], idx[a]] += w
    rows = P.sum(axis=1, keepdims=True)
    P = np.divide(P, rows, out=np.zeros_like(P), where=rows > 0)
    v = u.copy()
    for _ in range(iters):
        nxt = (1 - c) * P @ v + c * u
        if np.abs(nxt - v).sum() < tol:
            v = nxt
            break
        v = nxt
    return {x: float(v[i]) for i, x in enumerate(nodes)}


def b2_scores(view: View, win: Windows) -> tuple[dict, dict]:
    if "trace" in view.unavailable:
        return {}, {"reason": "traces unavailable: MicroRCA needs service-call response times"}
    (b0, _), (a0, a1) = win.baseline, win.analysis
    # attributed graph from the time frame *before* the anomaly (the baseline window)
    graph = {e for e, info in observed_graph(view).items() if info["first_ns"] is not None and info["first_ns"] < a0}
    if not graph:
        return {}, {"reason": "no observed service calls before the analysis window"}
    n_a = (a1 - a0) // BUCKET_NS
    mean_s = lambda rs: float(np.mean([r["value"] for r in rs])) / 1000.0   # response time in seconds
    edge_rt, anomalous = {}, set()
    for (u, v) in sorted(graph):
        calls = view.select(modality="trace", service=u, kind="CLIENT", peer=v)
        base = _bucket_values(calls, b0, a0, mean_s)
        ana = _bucket_values(calls, a0, a1, mean_s)
        edge_rt[(u, v)] = _series(calls, a0, a1, n_a, mean_s,
                                  fill=float(np.median(list(base.values()))) if base else 0.0)
        if len(base) < 3 or len(ana) < 2:
            continue
        xs = np.array(list(base.values()) + list(ana.values())).reshape(-1, 1)
        labels = Birch(threshold=B2_BIRCH_THRESHOLD, n_clusters=None).fit_predict(xs)
        base_clusters = set(labels[:len(base)])
        if sum(lab not in base_clusters for lab in labels[len(base):]) >= 2:   # slow responses form a new cluster
            anomalous.add((u, v))
    if not anomalous:
        return {}, {"reason": "BIRCH found no anomalous edge", "edges": len(graph)}
    anomalous_nodes = sorted({v for _, v in anomalous})       # callees of anomalous edges (paper's Fig. 2 example)
    rt_a = {j: np.mean([edge_rt[e] for e in anomalous if e[1] == j], axis=0) for j in anomalous_nodes}
    res = {s: [_series(view.select(service=s, measurement=m), a0, a1, n_a,
                       lambda rs: float(np.mean([r["value"] for r in rs]))) for m in ("container_cpu", "container_memory")]
           for s in ALL_SERVICES}
    host_u = [sum(res[s][k] for s in ALL_SERVICES) for k in range(2)]   # single VM host: sum over containers
    service_edges = set(graph)
    edges = service_edges | {(s, "host") for s in ALL_SERVICES}
    weights = {}
    # Algorithm 1: in-edges, then out-edges of each anomalous node; anomalous edges always get alpha
    for j in anomalous_nodes:
        for (i, jj) in sorted(service_edges):
            if jj == j and (i, j) not in weights:
                weights[(i, j)] = B2_ALPHA if (i, j) in anomalous else _corr(rt_a[j], edge_rt[(i, j)])
        w_in = [w for (i, jj), w in weights.items() if jj == j]
        for (jj, k) in sorted(edges):
            if jj != j or (j, k) in weights:
                continue
            if k == "host":
                weights[(j, k)] = (float(np.mean(w_in)) if w_in else 0.0) * max(_corr(rt_a[j], h) for h in host_u)
            else:
                weights[(j, k)] = B2_ALPHA if (j, k) in anomalous else _corr(rt_a[j], edge_rt[(j, k)])
    sub_nodes = sorted({x for e in weights for x in e} | set(anomalous_nodes))
    score_as = {}
    for j in anomalous_nodes:   # Eq. 3
        inc = [w for e, w in weights.items() if j in e]
        score_as[j] = (float(np.mean(inc)) if inc else 0.0) * max(_corr(rt_a[j], u_) for u_ in res[j])
    ppv = microrca_ppv(sub_nodes, [e for e in edges if e in weights], weights, score_as)
    ppv.pop("host", None)
    return ppv, {"anomalous_edges": sorted(f"{u}->{v}" for u, v in anomalous), "anomaly_scores": score_as}


def b2(view: View, win: Windows) -> dict:
    diag = empty_diagnosis(view, "B2", win)
    if not win.triggered:
        diag["uncertainty_reasons"].append("no alert: analysis window undefined")
        return diag
    scores, info = b2_scores(view, win)
    if not scores:
        diag["uncertainty_reasons"].append(info.get("reason", "no candidates"))
        return diag
    diag = finish(diag, scores)
    diag["_internal"] = info
    return diag


# ================================================================ B3: TraceRCA-inspired
B3_FS = 0.10    # δ_fs: feature selected if its anomalous fraction rises by more than this
B3_AD = 1.0     # δ_ad: invocation anomalous if a selected feature's severity exceeds this
B3_SPT = 0.10   # δ_spt: minimum support among anomalous traces
B3_TOPK = 100


def b3_scores(view: View, win: Windows) -> tuple[dict, dict]:
    if "trace" in view.unavailable:
        return {}, {"reason": "traces unavailable: TraceRCA needs traces"}
    (b0, _), (a0, a1) = win.baseline, win.analysis
    inv = [r for r in view.select(modality="trace", kind="CLIENT") if r["peer_service"] in ALL_SERVICES]
    base = [r for r in inv if b0 <= r["ts_start"] < a0]
    ana = [r for r in inv if a0 <= r["ts_start"] < a1]
    if not ana or not base:
        return {}, {"reason": "no invocations in one of the windows"}

    def pair(r):
        return (r["service"], r["peer_service"])

    def feat(r, f):
        return r["value"] if f == "latency" else (1.0 if r["status"] == "error" else 0.0)
    hist = defaultdict(lambda: defaultdict(list))
    for r in base:
        for f in ("latency", "status"):
            hist[pair(r)][f].append(feat(r, f))
    stats = {p: {f: (float(np.mean(v)), float(np.std(v))) for f, v in fs.items()} for p, fs in hist.items()}
    floors = {"latency": 1.0, "status": 0.05}   # sigma floor (ms / status units) for constant histories

    def severity(r, f):   # alpha = |v - mu| / sigma, per microservice pair
        if pair(r) not in stats:
            return float("inf")
        mu, sd = stats[pair(r)][f]
        return abs(feat(r, f) - mu) / max(sd, floors[f])
    useful = {}
    for p in sorted({pair(r) for r in ana}):
        sel = []
        for f in ("latency", "status"):
            before = [severity(r, f) for r in base if pair(r) == p]
            after = [severity(r, f) for r in ana if pair(r) == p]
            a_b = float(np.mean(before)) if before else 0.0
            a_a = float(np.mean(after))
            if a_a - a_b > B3_FS * a_b:   # paper: alpha_after - alpha_before > delta_fs * alpha_before
                sel.append(f)
        useful[p] = sel

    def abnormal(r):
        return any(severity(r, f) > B3_AD for f in useful.get(pair(r), ()))
    traces = defaultdict(lambda: {"services": set(), "in_abn": set(), "out_abn": set(), "abnormal": False})
    for r in ana:
        t = traces[r["trace_id"]]
        t["services"] |= {r["service"], r["peer_service"]}
        if abnormal(r):
            t["abnormal"] = True
            t["in_abn"].add(r["peer_service"])
            t["out_abn"].add(r["service"])
    abn = {tid for tid, t in traces.items() if t["abnormal"]}
    if not abn:
        return {}, {"reason": "no abnormal traces"}
    items = sorted({s for tid in abn for s in traces[tid]["services"]})
    # Frequent microservice sets among abnormal traces (support >= delta_spt). With <= 5 services, exhaustive
    # enumeration returns exactly the frequent itemsets FP-growth would (FP-growth only changes efficiency).
    sets = []
    for k in range(1, len(items) + 1):
        for S in combinations(items, k):
            x = {t for t, tr in traces.items() if set(S) <= tr["services"]}
            sup = len(x & abn) / len(abn)          # P(X | Y)
            if sup < B3_SPT:
                continue
            ji = len(x & abn) / len(x | abn)
            sets.append((ji, sup, S, x))
    sets.sort(key=lambda z: (-z[0], -z[1], len(z[2]), z[2]))
    sets = sets[:B3_TOPK]
    scores = {}
    for ji, _, S, x in sets:
        for m in S:   # in-set score: |#traces with incoming abnormal on m - #traces with outgoing abnormal on m|
            inc = sum(m in traces[t]["in_abn"] for t in x)
            out = sum(m in traces[t]["out_abn"] for t in x)
            scores[m] = max(scores.get(m, 0.0), ji * abs(inc - out))
    return scores, {"top_sets": [{"set": list(S), "jaccard": round(j, 4), "support": round(sp, 4)}
                                 for j, sp, S, _ in sets[:5]],
                    "useful_features": {f"{a}->{b}": v for (a, b), v in useful.items()},
                    "abnormal_traces": len(abn), "traces": len(traces)}


def b3(view: View, win: Windows) -> dict:
    diag = empty_diagnosis(view, "B3", win)
    if not win.triggered:
        diag["uncertainty_reasons"].append("no alert: analysis window undefined")
        return diag
    scores, info = b3_scores(view, win)
    if not scores:
        diag["uncertainty_reasons"].append(info.get("reason", "no candidates"))
        return diag
    diag = finish(diag, scores)
    diag["_internal"] = info
    return diag


__all__ = ["b1", "b2", "b3", "b1_scores", "b2_scores", "b3_scores", "microrca_ppv", "APP_SERVICES"]
