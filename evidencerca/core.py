"""Shared agent-side building blocks: robust statistics, time buckets, the SLO alert detector
(protocol §2), analysis/baseline windows, and the observed dependency graph.

Everything here is computed from a View only.
"""

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from evidencerca.evidence import ALL_SERVICES, View

BUCKET_NS = 5 * 10**9
STARTUP_SKIP_NS = 30 * 10**9      # first 30 s of any capture are a start-up transient; never used as reference
ANALYSIS_HALF_NS = 60 * 10**9     # analysis window = [alert - 60 s, alert + 60 s]
BASELINE_NS = 90 * 10**9          # baseline window = the 90 s before the analysis window
MAD_K = 1.4826


def robust(values) -> tuple[float, float]:
    """(median, scaled MAD)."""
    if len(values) == 0:
        return float("nan"), float("nan")
    a = np.asarray(values, dtype=float)
    med = float(np.median(a))
    return med, float(MAD_K * np.median(np.abs(a - med)))


def bucket_of(t: int, origin: int) -> int:
    return (t - origin) // BUCKET_NS


# ---------------------------------------------------------------- alert detector
@dataclass
class Windows:
    triggered: bool
    anchor: str                 # "alert" | "oracle"
    alert_ns: int | None
    analysis: tuple[int, int] | None
    baseline: tuple[int, int] | None
    detector_source: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _gateway_series(view: View):
    """Per 5 s bucket: (bucket_start_ns, request count, error fraction, p95 latency ms) from gateway SERVER spans,
    falling back to gateway app metrics when traces are unavailable."""
    origin = view.t_min
    if "trace" not in view.unavailable:
        spans = view.select(modality="trace", service="gateway", kind="SERVER")
        if spans:
            b = defaultdict(list)
            for s in spans:
                b[bucket_of(s["ts_start"], origin)].append(s)
            out = []
            for k in range(0, max(b) + 1):
                ss = b.get(k, [])
                if not ss:
                    out.append((origin + k * BUCKET_NS, 0, None, None))
                    continue
                d = [s["value"] for s in ss]
                out.append((origin + k * BUCKET_NS, len(ss), sum(s["status"] == "error" for s in ss) / len(ss),
                            float(np.percentile(d, 95))))
            return out, "gateway SERVER spans"
    if "metric" not in view.unavailable:
        lat = {bucket_of(r["ts_end"], origin): r["value"] for r in view.select(service="gateway", measurement="latency_mean")}
        err = {bucket_of(r["ts_end"], origin): r["value"] for r in view.select(service="gateway", measurement="error_ratio")}
        if lat:
            return [(origin + k * BUCKET_NS, 1, err.get(k, 0.0), lat[k]) for k in sorted(lat)], "gateway app metrics"
    return [], None


def detect_alert(view: View, consecutive: int = 3) -> Windows:
    series, source = _gateway_series(view)
    if not series:
        return Windows(False, "alert", None, None, None, None, "no gateway telemetry in this view")
    origin = series[0][0]
    for i, (t, n, err, p95) in enumerate(series):
        if t < origin + STARTUP_SKIP_NS + 60 * 10**9:
            continue
        ref = [x for x in series if origin + STARTUP_SKIP_NS <= x[0] and t - 90 * 10**9 <= x[0] < t - 30 * 10**9
               and x[2] is not None]
        if len(ref) < 6:
            continue
        e_med, e_sd = robust([x[2] for x in ref])
        p_med, p_sd = robust([x[3] for x in ref])
        e_thr = e_med + max(0.05, 3 * e_sd)
        p_thr = max(2 * p_med, p_med + 3 * p_sd + 50.0)

        def hot(x):
            return x[2] is not None and (x[2] >= e_thr or x[3] >= p_thr)
        run = series[i:i + consecutive]
        if len(run) == consecutive and all(hot(x) for x in run):
            return make_windows(view, t, "alert", source,
                                f"gateway err>={e_thr:.3f} or p95>={p_thr:.1f}ms in {consecutive} consecutive buckets")
    return Windows(False, "alert", None, None, None, source, "no SLO deviation detected")


def make_windows(view: View, anchor_ns: int, anchor: str, source=None, reason=None) -> Windows:
    a0, a1 = anchor_ns - ANALYSIS_HALF_NS, anchor_ns + ANALYSIS_HALF_NS
    b0 = max(a0 - BASELINE_NS, view.t_min + STARTUP_SKIP_NS)
    return Windows(True, anchor, anchor_ns, (a0, a1), (b0, a0), source, reason)


# ---------------------------------------------------------------- observed dependency graph
def observed_graph(view: View) -> dict:
    """Directed call edges caller -> callee observed in the view, with counts and first/last seen.

    Edges come only from telemetry: a CLIENT span whose child SERVER span is in another service, a CLIENT
    span's peer (server.address) when the child is missing, and DB client spans (-> postgres)."""
    edges = defaultdict(lambda: {"count": 0, "first_ns": None, "last_ns": None})
    if "trace" in view.unavailable:
        return {}
    spans = view.select(modality="trace")
    by_id = {s["span_id"]: s for s in spans}

    def add(a, b, t):
        e = edges[(a, b)]
        e["count"] += 1
        e["first_ns"] = t if e["first_ns"] is None else min(e["first_ns"], t)
        e["last_ns"] = t if e["last_ns"] is None else max(e["last_ns"], t)

    seen_clients = set()
    for s in spans:
        if s["kind"] == "SERVER" and s["parent_span_id"] in by_id:
            p = by_id[s["parent_span_id"]]
            if p["kind"] == "CLIENT" and p["service"] != s["service"]:
                add(p["service"], s["service"], p["ts_start"])
                seen_clients.add(p["span_id"])
    for s in spans:
        if s["kind"] == "CLIENT" and s["span_id"] not in seen_clients and s["peer_service"] in ALL_SERVICES \
                and s["peer_service"] != s["service"]:
            add(s["service"], s["peer_service"], s["ts_start"])
    return dict(edges)
