"""Method-neutral evidence pack: what an LLM sees of a View (agent side, no ground truth).

A View holds ~50k records; a 4B model with a ~5k-token context cannot read them. The pack is a bounded,
deterministic summary built identically for every service (no ranking, no anomaly score):

  digest   per service and record family, baseline-window vs analysis-window descriptive statistics
           (counts, median, p95/max, errors, timeouts, error-severity logs, max metric inter-sample gap)
  graph    observed caller -> callee edges (evidencerca.core.observed_graph, frozen)
  records  a table of at most `max_records` citable records. Neutral selection, applied to every service
           and family in the same order: the analysis-window maximum, the first error, and the first record
           above the family's own baseline median + 3 MAD; services are interleaved round-robin.

B4 gets exactly this pack. E1 gets the same digest and graph; its record table starts with the records that
EvidenceRCA (E0) retrieved and is filled with the neutral selection up to the same `max_records`, so both
conditions see the same number of records under the same token budget.
"""

import statistics
from dataclasses import dataclass, field

from evidencerca.core import observed_graph, robust
from evidencerca.evidence import ALL_SERVICES, View

SEC = 10**9
MAX_RECORDS = 36
PERIODIC = ("container_cpu", "process_cpu")
METRICS = ("container_cpu", "process_cpu", "latency_mean", "error_ratio", "pool_checked_out")


@dataclass
class Pack:
    alert_ns: int
    analysis: tuple
    baseline: tuple
    unavailable: list
    digest: list = field(default_factory=list)       # lines
    graph: list = field(default_factory=list)        # (caller, callee, count)
    records: list = field(default_factory=list)      # (evidence_id, tag, record)

    @property
    def record_ids(self) -> list[str]:
        return [r["evidence_id"] for _, _, r in self.records]


def _fmt(x, nd=1):
    if x is None:
        return "-"
    if isinstance(x, float) and (x != x):
        return "-"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def _p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.95 * len(xs)))] if xs else None


def _med(xs):
    return statistics.median(xs) if xs else None


def families(view: View, s: str):
    """(family name, predicate over records) for service s, in a fixed order."""
    return [
        ("server spans", lambda r: r["modality"] == "trace" and r["kind"] == "SERVER" and r["service"] == s),
        ("inbound calls", lambda r: r["modality"] == "trace" and r["kind"] == "CLIENT" and r["peer_service"] == s
         and r["service"] != s),
        ("pool connect", lambda r: r["modality"] == "trace" and r["kind"] == "CLIENT" and r["service"] == s
         and r["operation"] == "connect" and r["peer_service"] is None),
        ("error logs", lambda r: r["modality"] == "log" and r["service"] == s and (r["severity"] or 0) >= 17),
    ] + [(f"metric {m}", (lambda m: lambda r: r["modality"] == "metric" and r["service"] == s
                          and r["measurement"] == m)(m)) for m in METRICS]


def _max_gap_s(view, s, meas, w0, w1):
    ts = sorted(r["ts_start"] for r in view.select(modality="metric", service=s, measurement=meas))
    if not ts:
        return None
    prior = [t for t in ts if t < w0]
    pts = ([prior[-1]] if prior else [w0]) + [t for t in ts if w0 <= t < w1] + [w1]
    return max(b - a for a, b in zip(pts, pts[1:])) / SEC


def median_time(view: View) -> int:
    """Label-free window anchor for a view without an alert: the median record timestamp (robust to the few
    start-up records that precede the workload by minutes)."""
    ts = sorted(r["ts_start"] for r in view.records)
    return ts[len(ts) // 2]


def build(view: View, win, first_ids: list[str] | None = None, max_records: int = MAX_RECORDS) -> Pack:
    (b0, b1), (a0, a1) = win.baseline, win.analysis
    pk = Pack(alert_ns=win.alert_ns, analysis=(a0, a1), baseline=(b0, b1), unavailable=sorted(view.unavailable))
    by_service = {s: [r for r in view.records if r["service"] == s or r.get("peer_service") == s]
                  for s in ALL_SERVICES}
    picks = {s: [] for s in ALL_SERVICES}
    for s in ALL_SERVICES:
        for fam, pred in families(view, s):
            recs = [r for r in by_service[s] if pred(r)]
            base = [r for r in recs if b0 <= r["ts_start"] < b1]
            win_ = [r for r in recs if a0 <= r["ts_start"] < a1]
            if not base and not win_:
                continue
            bv, wv = [r["value"] for r in base], [r["value"] for r in win_]
            unit = (win_ or base)[0]["unit"]
            if fam == "error logs":
                line = f"{s} | {fam} | baseline n={len(base)} | analysis n={len(win_)}"
            elif fam.startswith("metric"):
                line = (f"{s} | {fam} ({unit}) | baseline n={len(base)} median={_fmt(_med(bv), 3)} | analysis n={len(win_)}"
                        f" median={_fmt(_med(wv), 3)} max={_fmt(max(wv) if wv else None, 3)}")
                meas = fam.split()[1]
                if meas in PERIODIC:
                    line += f" max_gap_s={_fmt(_max_gap_s(view, s, meas, a0, a1), 2)}"
            else:
                be, we = sum(r["status"] == "error" for r in base), sum(r["status"] == "error" for r in win_)
                to = sum("timeout" in (r["error_type"] or "").lower() for r in win_)
                line = (f"{s} | {fam} (ms) | baseline n={len(base)} median={_fmt(_med(bv))} errors={be} | analysis "
                        f"n={len(win_)} median={_fmt(_med(wv))} p95={_fmt(_p95(wv))} errors={we}"
                        + (f" error_ratio={we / len(win_):.4f}" if win_ else "") + (f" timeouts={to}" if fam == "inbound calls" else ""))
            pk.digest.append(line)
            # neutral record selection for this family
            cand = []
            if win_:
                cand.append(("max", max(win_, key=lambda r: (r["value"], r["evidence_id"]))))
                errs = sorted((r for r in win_ if r["status"] == "error"), key=lambda r: (r["ts_start"], r["evidence_id"]))
                if errs:
                    cand.append(("first error", errs[0]))
                if len(bv) >= 3:
                    m, mad = robust(bv)
                    above = sorted((r for r in win_ if r["value"] > m + 3 * mad and r["value"] > m),
                                   key=lambda r: (r["ts_start"], r["evidence_id"]))
                    if above:
                        cand.append(("first above baseline", above[0]))
            picks[s].extend(cand)
    pk.graph = sorted((a, b, e["count"]) for (a, b), e in observed_graph(view).items())
    # same table size for every condition: the number of distinct neutral picks, capped at max_records
    size = min(max_records, len({r["evidence_id"] for s in ALL_SERVICES for _, r in picks[s]}))
    chosen, seen = [], set()
    for eid in first_ids or []:
        r = view.by_id.get(eid)
        if r is not None and eid not in seen and len(chosen) < size:
            chosen.append(("retrieved by EvidenceRCA", r))
            seen.add(eid)
    queues = {s: list(picks[s]) for s in ALL_SERVICES}
    while len(chosen) < size and any(queues.values()):
        for s in ALL_SERVICES:
            while queues[s]:
                tag, r = queues[s].pop(0)
                if r["evidence_id"] not in seen:
                    chosen.append((tag, r))
                    seen.add(r["evidence_id"])
                    break
            if len(chosen) >= size:
                break
    chosen.sort(key=lambda x: (x[1]["ts_start"], x[1]["evidence_id"]))
    pk.records = [(r["evidence_id"], tag, r) for tag, r in chosen]
    return pk


def render_record(r: dict, alert_ns: int) -> str:
    t = (r["ts_start"] - alert_ns) / SEC
    if r["modality"] == "trace":
        who = f"{r['service']} -> {r['peer_service']}" if r["kind"] == "CLIENT" and r["peer_service"] else r["service"]
        what = f"trace {r['kind']} {r['operation']}"
        val = f"{r['value']:.1f} ms"
        st = r["status"] + (f" {r['error_type']}" if r["error_type"] else "") + \
            (f" http={r['http_status']}" if r["http_status"] else "")
    elif r["modality"] == "log":
        who, what = r["service"], f"log severity={r['severity']}"
        val = repr((r["text"] or "")[:70])
        st = r["status"]
    else:
        who, what = r["service"], f"metric {r['measurement']}"
        val = f"{r['value']:.4g} {r['unit']}"
        st = ""
    return f"{r['evidence_id']} | t={t:+.3f}s | {who} | {what} | {val} | {st}".rstrip(" |")


def render(pk: Pack) -> dict:
    """Case-dependent prompt sections (text). Times are seconds relative to the window anchor."""
    a0, a1 = pk.analysis
    b0, b1 = pk.baseline
    rel = lambda t: (t - pk.alert_ns) / SEC
    head = (f"Analysis window: t={rel(a0):+.0f}s to t={rel(a1):+.0f}s; baseline window: t={rel(b0):+.0f}s to "
            f"t={rel(b1):+.0f}s (t=0 is the window anchor).\n"
            f"Unavailable telemetry modalities: {', '.join(pk.unavailable) if pk.unavailable else 'none'}.")
    graph = "\n".join(f"{a} -> {b} ({n} calls)" for a, b, n in pk.graph) or "(no call edges observed)"
    table = "\n".join(f"[{tag}] " + render_record(r, pk.alert_ns) for _, tag, r in pk.records) or "(none)"
    return {"header": head, "digest": "\n".join(pk.digest) or "(no telemetry)", "graph": graph, "records": table}
