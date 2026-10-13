"""EvidenceRCA, deterministic configuration (E0: template claims, no LLM).

detect (shared alert/windows) -> per-service signals (evidencerca.signals) -> temporal onsets ->
observed-graph-constrained ranking -> evidence retrieval -> typed claims + propagation path.

Graph constraint: a service s is *propagated* if it calls (observed edge s -> c) a dependency c that
shows a caller-observed or intrinsic anomaly whose onset is no later than s's onset + 10 s. For a
propagated service only intrinsic signals (silence, container CPU) count fully and pool occupancy
counts at half weight; its latency/error/log anomalies are treated as symptoms.

`use_graph=False` gives the A-graph ablation: no propagation discount and no propagation path.
"""

from evidencerca.core import Windows, observed_graph
from evidencerca.evidence import ALL_SERVICES, View
from evidencerca.methods.diagnosis import claim, empty_diagnosis, finish
from evidencerca.signals import evaluate_all

ONSET_TOLERANCE_NS = 10 * 10**9
CALLEE_SIDE = ("callee_latency", "callee_failures", "callee_timeouts")   # observed by callers, located at callee
INTRINSIC = ("silence", "container_cpu")
POOL = ("pool_checked_out", "pool_wait")   # half weight when the service is itself explained by a dependency
SYMPTOM = ("server_latency", "server_errors", "error_logs", "latency_mean", "error_ratio", "slow_statements")
MAX_CITED = 5

# signal -> (predicate, measurement, unit, how the claim value is formed)
CLAIM_OF = {
    "server_latency": ("latency_above_baseline", "span_duration", "ms", "median_cited"),
    "callee_latency": ("latency_above_baseline", "span_duration", "ms", "median_cited"),
    "latency_mean": ("latency_above_baseline", "latency_mean", "ms", "median_cited"),
    "server_errors": ("error_rate_above_baseline", "span_error_ratio", "1", "window_rate"),
    "error_ratio": ("error_rate_above_baseline", "error_ratio", "1", "median_cited"),
    "callee_failures": ("call_failed", "span_duration", "{call}", "window_count"),
    "callee_timeouts": ("timeout_observed", "span_duration", "{call}", "window_count"),
    "silence": ("no_telemetry_from", "periodic_stream_gap", "s", "gap"),
    "container_cpu": ("cpu_above_baseline", "container_cpu", "%", "median_cited"),
    "pool_checked_out": ("pool_wait_above_baseline", "pool_checked_out", "{connection}", "median_cited"),
    "pool_wait": ("pool_wait_above_baseline", "span_duration", "ms", "median_cited"),
    "error_logs": ("log_error_pattern", "log_record", "{record}", "window_count"),
}


def _onset(sigs: dict, names) -> int | None:
    on = [sigs[n].onset_ns for n in names if n in sigs and sigs[n].anomalous and sigs[n].onset_ns is not None]
    return min(on) if on else None


def rank(view: View, win: Windows, use_graph: bool = True) -> tuple[dict, dict, dict]:
    """(scores, per-service ranking detail, signals). Exposed for per-modality rankings and abstention."""
    signals = evaluate_all(view, win)
    graph = observed_graph(view) if use_graph else {}
    callees = {}
    for (a, b) in graph:
        callees.setdefault(a, set()).add(b)
    detail, scores = {}, {}
    for s in ALL_SERVICES:
        sig = signals[s]
        own_onset = _onset(sig, sig.keys())
        explained_by = []
        if use_graph and own_onset is not None:
            for c in sorted(callees.get(s, ())):
                c_onset = _onset(signals[c], CALLEE_SIDE + INTRINSIC + POOL)
                if c_onset is not None and c_onset <= own_onset + ONSET_TOLERANCE_NS:
                    explained_by.append(c)
        counted = []
        for name, sg in sig.items():
            if not sg.anomalous:
                continue
            w = 1.0
            if explained_by:
                if name in INTRINSIC:
                    w = 1.0
                elif name in POOL:
                    w = 0.5
                else:
                    continue
            counted.append((name, sg.strength * w))
        score = (max(v for _, v in counted) + 2.0 * (len(counted) - 1)) if counted else 0.0
        scores[s] = score
        detail[s] = {"onset_ns": own_onset, "explained_by": explained_by, "counted": counted}
    return scores, detail, signals


def infer_fault_type(sig: dict, service: str) -> str:
    if service == "postgres":
        return "cascading_failure"
    if sig["silence"].anomalous:
        return "service_crash"
    if sig["pool_checked_out"].anomalous or sig["pool_wait"].anomalous:
        return "db_pool_exhaustion"
    if sig["container_cpu"].anomalous:
        return "cpu_pressure"
    if sig["callee_timeouts"].anomalous or (sig["callee_failures"].anomalous and not sig["server_errors"].anomalous):
        return "network_timeout"
    return "processing_latency"


def _operation(sig: dict) -> str | None:
    recs = sig["server_latency"].records if sig["server_latency"].anomalous else []
    if not recs:
        return None
    ops = {}
    for r in recs:
        ops[r["operation"]] = ops.get(r["operation"], 0) + 1
    return max(sorted(ops), key=ops.get)


def _cite(records, n=MAX_CITED, by_value=True):
    rs = sorted(records, key=lambda r: (-r["value"], r["evidence_id"]) if by_value else (r["ts_start"],
                                                                                        r["evidence_id"]))
    return rs[:n]


def make_claims(view: View, win: Windows, root: str, sig: dict, detail: dict, signals: dict) -> list[dict]:
    out, n = [], 0
    a0, a1 = win.analysis
    for name in sorted(sig):
        sg = sig[name]
        if not sg.anomalous or name not in CLAIM_OF:
            continue
        predicate, meas, unit, how = CLAIM_OF[name]
        if how == "gap":
            cited = sg.records
            value = round(max(d["max_gap_s"] for d in sg.detail.values() if d.get("status") == "missing"), 3)
        elif how == "median_cited":
            cited = _cite(sg.records)
            vals = sorted(r["value"] for r in cited)
            value = round(vals[len(vals) // 2], 6) if vals else None
        elif how == "window_rate":
            pool = [r for r in view.select(modality="trace", service=root, kind="SERVER", t0=a0, t1=a1)]
            cited = _cite([r for r in pool if r["status"] == "error"], by_value=False)
            value = round(sum(r["status"] == "error" for r in pool) / len(pool), 6) if pool else None
        else:  # window_count
            if name == "callee_failures":
                pool = [r for r in view.select(modality="trace", kind="CLIENT", peer=root, t0=a0, t1=a1)
                        if r["status"] == "error"]
            elif name == "callee_timeouts":
                pool = [r for r in view.select(modality="trace", kind="CLIENT", peer=root, t0=a0, t1=a1)
                        if "timeout" in (r["error_type"] or "").lower()]
            else:
                pool = [r for r in view.select(modality="log", service=root, t0=a0, t1=a1) if r["severity"] >= 17]
            cited = _cite(pool, by_value=False)
            value = len(pool)
        if not cited or value is None:
            continue
        n += 1
        out.append(claim(f"c{n}", "observed", root, predicate, value, unit, (a0, a1),
                         [r["evidence_id"] for r in cited], measurement=meas))
    # Temporal claims: the root's onset precedes each propagated neighbour's onset.
    root_onset = detail[root]["onset_ns"]
    for s, d in sorted(detail.items()):
        if s == root or root not in d["explained_by"] or d["onset_ns"] is None or root_onset is None:
            continue
        if root_onset <= d["onset_ns"]:
            first_root = _first_anomalous(signals[root])
            first_s = _first_anomalous(signals[s])
            if first_root and first_s:
                # lead measured between the cited records themselves (record time resolution, not 5 s buckets)
                n += 1
                out.append(claim(f"c{n}", "observed", root, "onset_before",
                                 round((first_s["ts_start"] - first_root["ts_start"]) / 1e9, 3), "s", (a0, a1),
                                 [first_root["evidence_id"], first_s["evidence_id"]], obj=s))
    # One inferred causal hypothesis; it cites premises only and is never treated as proof.
    premises = [e for c in out[:3] for e in c["evidence_ids"][:1]]
    if premises:
        n += 1
        out.append(claim(f"c{n}", "inferred", root, out[0]["predicate"], None, None, (a0, a1), premises,
                         measurement=out[0]["measurement"]))
    return out


def _first_anomalous(sig: dict):
    cands = [r for sg in sig.values() if sg.anomalous for r in sg.records]
    return min(cands, key=lambda r: (r["ts_start"], r["evidence_id"])) if cands else None


def propagation_path(view: View, win: Windows, root: str, detail: dict) -> list[dict]:
    """Edges from the root towards services whose anomalies it explains, following observed caller edges."""
    a0, a1 = win.analysis
    path, frontier, seen = [], [root], {root}
    while frontier:
        nxt = []
        for c in frontier:
            for s, d in sorted(detail.items()):
                if s in seen or c not in d["explained_by"]:
                    continue
                calls = [r for r in view.select(modality="trace", service=s, kind="CLIENT", peer=c, t0=a0, t1=a1)]
                calls.sort(key=lambda r: (-(r["status"] == "error"), -r["value"], r["evidence_id"]))
                path.append({"from": c, "to": s, "evidence_ids": [r["evidence_id"] for r in calls[:3]]})
                seen.add(s)
                nxt.append(s)
        frontier = nxt
    return path


def diagnose(view: View, win: Windows, use_graph: bool = True) -> dict:
    return diagnose_full(view, win, use_graph)[0]


def diagnose_full(view: View, win: Windows, use_graph: bool = True) -> tuple[dict, dict | None]:
    """(diagnosis, signals); signals feed the abstention gate (sufficiency, modality agreement)."""
    method = "E0" if use_graph else "E0-A-graph"
    diag = empty_diagnosis(view, method, win)
    if not win.triggered:
        diag["uncertainty_reasons"].append("no alert: analysis window undefined")
        return diag, None
    scores, detail, signals = rank(view, win, use_graph)
    finish(diag, scores)
    root = diag["suspected_service"]
    if root is None:
        return diag, signals
    sig = signals[root]
    diag["suspected_fault_type"] = infer_fault_type(sig, root)
    diag["suspected_operation"] = _operation(sig)
    diag["factual_claims"] = make_claims(view, win, root, sig, detail, signals)
    diag["propagation_path"] = propagation_path(view, win, root, detail) if use_graph else []
    diag["evidence_ids"] = sorted({e for c in diag["factual_claims"] for e in c["evidence_ids"]})
    diag["_internal"] = {"detail": {s: {k: v for k, v in d.items() if k != "counted"} | {
        "counted": [[n, round(x, 3)] for n, x in d["counted"]]} for s, d in detail.items()},
        "signal_status": {s: {n: sg.status for n, sg in sig_.items()} for s, sig_ in signals.items()}}
    return diag, signals
