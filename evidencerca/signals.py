"""Per-service signals for one view and window pair (agent side).

A *signal* is a per-5 s-bucket series for one service, compared between the baseline and the
analysis window with a robust threshold:
    threshold = median(baseline) + max(3 * scaled MAD(baseline), floor)
A signal is anomalous if at least 2 consecutive analysis buckets exceed the threshold; its onset
is the start of the first such bucket; its strength is the largest exceedance in robust units
(capped at 50).

Silence (`no telemetry`) is never inferred from one missing record: a periodic stream must have an
established cadence in the baseline (>= 6 records, median interval D), and the analysis window must
contain a gap >= 3 D with no record of that stream.

Observation status per (service, signal): observed_normal | observed_anomalous | missing |
unavailable (modality not collected in this view) | no_baseline | no_analysis_data (the series has
no records in the analysis window, e.g. a service that received no calls; not evidence either way).
"""

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from evidencerca.core import BUCKET_NS, Windows, robust
from evidencerca.evidence import ALL_SERVICES, APP_SERVICES, View

STRENGTH_CAP = 50.0
SILENCE_STRENGTH = 50.0
PERIODIC = ("container_cpu", "process_cpu")   # exported every 5 s regardless of traffic


@dataclass
class Signal:
    service: str
    name: str
    modality: str
    status: str
    onset_ns: int | None = None
    strength: float = 0.0
    threshold: float | None = None
    baseline_median: float | None = None
    records: list = field(default_factory=list, repr=False)      # analysis-window records exceeding threshold
    detail: dict = field(default_factory=dict)

    @property
    def anomalous(self) -> bool:
        return self.status in ("observed_anomalous", "missing")


# signal name -> (modality, record selector, per-bucket aggregate, floor, unit)
def _p95(rs):
    return float(np.percentile([r["value"] for r in rs], 95))


def _err_frac(rs):
    return sum(r["status"] == "error" for r in rs) / len(rs)


def _timeout_frac(rs):
    return sum("timeout" in (r["error_type"] or "").lower() for r in rs) / len(rs)


def _count(rs):
    return float(len(rs))


def _mean(rs):
    return float(np.mean([r["value"] for r in rs]))


def _max(rs):
    return float(max(r["value"] for r in rs))


SPECS = {
    "server_latency":   ("trace", lambda v, s: v.select(modality="trace", service=s, kind="SERVER"), _p95, 25.0),
    "server_errors":    ("trace", lambda v, s: v.select(modality="trace", service=s, kind="SERVER"), _err_frac, 0.05),
    "callee_latency":   ("trace", lambda v, s: v.select(modality="trace", kind="CLIENT", peer=s), _p95, 25.0),
    "callee_failures":  ("trace", lambda v, s: v.select(modality="trace", kind="CLIENT", peer=s), _err_frac, 0.05),
    "callee_timeouts":  ("trace", lambda v, s: v.select(modality="trace", kind="CLIENT", peer=s), _timeout_frac, 0.05),
    "error_logs":       ("log", lambda v, s: [r for r in v.select(modality="log", service=s) if r["severity"] >= 17],
                         _count, 2.0),
    "slow_statements":  ("log", lambda v, s: [r for r in v.select(modality="log", service=s)
                                              if "duration:" in (r["text"] or "")] if s == "postgres" else [],
                         _count, 2.0),
    "pool_wait":        ("trace", lambda v, s: [r for r in v.select(modality="trace", service=s, kind="CLIENT")
                                            if r["operation"] == "connect" and r["peer_service"] is None],
                         _p95, 25.0),
    "container_cpu":    ("metric", lambda v, s: v.select(service=s, measurement="container_cpu"), _mean, 10.0),
    "pool_checked_out": ("metric", lambda v, s: v.select(service=s, measurement="pool_checked_out"), _max, 1.0),
    "latency_mean":     ("metric", lambda v, s: v.select(service=s, measurement="latency_mean"), _mean, 25.0),
    "error_ratio":      ("metric", lambda v, s: v.select(service=s, measurement="error_ratio"), _mean, 0.05),
}
COUNT_SIGNALS = {"error_logs", "slow_statements"}          # zero is a valid value for empty buckets
VALUE_OF = {"server_latency": "value", "callee_latency": "value"}  # per-record value used to pick evidence


def _buckets(records, t0, t1):
    b = defaultdict(list)
    for r in records:
        if t0 <= r["ts_start"] < t1:
            b[(r["ts_start"] - t0) // BUCKET_NS].append(r)
    return b


def evaluate_signal(view: View, win: Windows, service: str, name: str) -> Signal:
    modality, select, agg, floor = SPECS[name]
    if modality in view.unavailable:
        return Signal(service, name, modality, "unavailable")
    recs = select(view, service)
    (b0, b1), (a0, a1) = win.baseline, win.analysis
    base_b, ana_b = _buckets(recs, b0, b1), _buckets(recs, a0, a1)
    n_base = (b1 - b0) // BUCKET_NS
    n_ana = (a1 - a0) // BUCKET_NS
    if name in COUNT_SIGNALS:
        base_vals = [agg(base_b.get(k, [])) if base_b.get(k) else 0.0 for k in range(n_base)]
    else:
        base_vals = [agg(base_b[k]) for k in sorted(base_b)]
    if len(base_vals) < 3:
        return Signal(service, name, modality, "no_baseline", detail={"baseline_buckets": len(base_vals)})
    med, sd = robust(base_vals)
    thr = med + max(3 * sd, floor)
    scale = max(sd, floor / 3)
    hot, run, onset, strength = [], 0, None, 0.0
    for k in range(n_ana):
        rs = ana_b.get(k, [])
        if name in COUNT_SIGNALS:
            x = agg(rs) if rs else 0.0
        elif not rs:
            run = 0
            continue
        else:
            x = agg(rs)
        if x > thr:
            run += 1
            strength = max(strength, (x - med) / scale)
            hot.append((k, rs))
            if run >= 2 and onset is None:
                onset = a0 + (k - 1) * BUCKET_NS
        else:
            run = 0
    if onset is not None:
        status = "observed_anomalous"
    elif ana_b or name in COUNT_SIGNALS:
        status = "observed_normal"
    else:
        status = "no_analysis_data"   # e.g. a service nobody called: neither normal nor missing
    exceed = []
    for _, rs in hot:
        exceed.extend(r for r in rs if name not in VALUE_OF or r["value"] > thr)
    return Signal(service, name, modality, status, onset, min(strength, STRENGTH_CAP) if onset else 0.0, thr, med,
                  exceed if onset else [], {"baseline_buckets": len(base_vals), "analysis_buckets": len(ana_b)})


def evaluate_silence(view: View, win: Windows, service: str) -> Signal:
    if "metric" in view.unavailable:
        return Signal(service, "silence", "metric", "unavailable")
    (b0, b1), (a0, a1) = win.baseline, win.analysis
    per_stream = {}
    for meas in PERIODIC:
        ts = sorted(r["ts_start"] for r in view.select(service=service, measurement=meas))
        base = [t for t in ts if b0 <= t < b1]
        if len(base) < 6:
            per_stream[meas] = {"status": "no_baseline", "baseline_records": len(base)}
            continue
        d = float(np.median(np.diff(base)))
        before = [t for t in ts if t < a0]
        inside = [t for t in ts if a0 <= t < a1]
        edges = ([before[-1]] if before else [a0]) + inside + [a1]
        gaps = [(edges[i + 1] - edges[i], edges[i]) for i in range(len(edges) - 1)]
        gap, start = max(gaps)
        per_stream[meas] = {"status": "missing" if gap >= 3 * d else "observed_normal", "cadence_s": d / 1e9,
                            "max_gap_s": gap / 1e9, "gap_start_ns": start}
    missing = [m for m, s in per_stream.items() if s["status"] == "missing"]
    known = [m for m, s in per_stream.items() if s["status"] != "no_baseline"]
    if not known:
        return Signal(service, "silence", "metric", "no_baseline", detail=per_stream)
    if missing and len(missing) == len(known):
        onset = min(per_stream[m]["gap_start_ns"] for m in missing) + int(min(
            per_stream[m]["cadence_s"] for m in missing) * 1e9)
        last = [r for r in view.select(service=service) if r["measurement"] in missing and r["ts_start"] < onset]
        last.sort(key=lambda r: r["ts_start"])
        return Signal(service, "silence", "metric", "missing", onset, SILENCE_STRENGTH, None, None, last[-2:],
                      per_stream)
    return Signal(service, "silence", "metric", "observed_normal", detail=per_stream)


def evaluate_all(view: View, win: Windows) -> dict:
    """service -> signal name -> Signal, for all services and all signals."""
    out = {}
    for s in ALL_SERVICES:
        sig = {n: evaluate_signal(view, win, s, n) for n in SPECS}
        sig["silence"] = evaluate_silence(view, win, s)
        if s == "postgres":   # no own spans/app metrics; signals that cannot exist are marked unavailable
            for n in ("server_latency", "server_errors", "latency_mean", "error_ratio", "pool_checked_out"):
                sig[n] = Signal(s, n, SPECS[n][0], "unavailable", detail={"reason": "not emitted by postgres"})
        if s not in ("order", "inventory"):
            for n in ("pool_checked_out", "pool_wait"):
                sig[n] = Signal(s, n, SPECS[n][0], "unavailable", detail={"reason": "no DB pool in this service"})
        out[s] = sig
    return out


__all__ = ["Signal", "SPECS", "evaluate_all", "evaluate_signal", "evaluate_silence", "APP_SERVICES"]
