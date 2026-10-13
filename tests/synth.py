"""Synthetic evidence for fast, Docker-free unit tests (not a model of the real testbed's numbers).

Call graph per request: gateway -> order -> {inventory, payment}; order and inventory -> postgres.
One request every 0.5 s for 300 s; container/process metrics every 5 s; one INFO log per server span.
Faults start at t = 150 s on `target`: latency (+500 ms server time), crash (no spans/metrics/logs,
callers' calls fail fast), pool (local connect spans slow + pool gauge at 5).
"""

import random

from evidencerca.evidence import View, assign_ids

T0 = 1_800_000_000_000_000_000
SEC = 10**9
CALLS = {"gateway": ["order"], "order": ["inventory", "payment", "postgres"], "inventory": ["postgres"],
         "payment": []}


def _rec(**kw):
    base = dict.fromkeys(("evidence_id", "case_id", "modality", "source", "service", "operation", "ts_start",
                          "ts_end", "measurement", "value", "unit", "trace_id", "span_id", "parent_span_id",
                          "kind", "peer_service", "status", "http_status", "error_type", "severity", "text"))
    base.update(kw)
    return base


def make_records(fault=None, target=None, seed=0, duration_s=300, onset_s=150):
    rng = random.Random(seed)
    out, sid = [], [0]

    def nid():
        sid[0] += 1
        return f"{sid[0]:016x}"

    def faulty(svc, t):
        return fault is not None and svc == target and t >= T0 + onset_s * SEC

    def serve(svc, t, trace, parent):
        """Emit SERVER span for svc starting at t (and its downstream calls); return (end, ok)."""
        if faulty(svc, t) and fault == "crash":
            return t + int(0.002 * SEC), False
        span = nid()
        cur = t + int(0.001 * SEC)
        ok = True
        for callee in CALLS[svc]:
            c_span = nid()
            if callee == "postgres":
                if faulty(svc, t) and fault == "pool":
                    out.append(_rec(modality="trace", source="otlp", service=svc, operation="connect", ts_start=cur,
                                    ts_end=cur + 2 * SEC, measurement="span_duration", value=2000.0, unit="ms",
                                    trace_id=trace, span_id=nid(), parent_span_id=span, kind="CLIENT",
                                    peer_service=None, status="error", error_type="TimeoutError"))
                    cur += 2 * SEC
                    ok = False
                    break
                d = int((0.001 + rng.random() * 0.001) * SEC)
                out.append(_rec(modality="trace", source="otlp", service=svc, operation="SELECT shop", ts_start=cur,
                                ts_end=cur + d, measurement="span_duration", value=d / 1e6, unit="ms",
                                trace_id=trace, span_id=c_span, parent_span_id=span, kind="CLIENT",
                                peer_service="postgres", status="ok"))
                cur += d
                continue
            end, c_ok = serve(callee, cur, trace, c_span)
            out.append(_rec(modality="trace", source="otlp", service=svc, operation="POST", ts_start=cur, ts_end=end,
                            measurement="span_duration", value=(end - cur) / 1e6, unit="ms", trace_id=trace,
                            span_id=c_span, parent_span_id=span, kind="CLIENT", peer_service=callee,
                            status="ok" if c_ok else "error", http_status=200 if c_ok else None,
                            error_type=None if c_ok else "ConnectError"))
            cur = end
            ok = ok and c_ok
        work = 0.003 + rng.random() * 0.004 + (0.5 if faulty(svc, t) and fault == "latency" else 0.0)
        end = cur + int(work * SEC)
        out.append(_rec(modality="trace", source="otlp", service=svc, operation=f"/{svc}", ts_start=t, ts_end=end,
                        measurement="span_duration", value=(end - t) / 1e6, unit="ms", trace_id=trace, span_id=span,
                        parent_span_id=parent, kind="SERVER", status="ok" if ok else "error",
                        http_status=200 if ok else 500))
        out.append(_rec(modality="log", source="otlp", service=svc, operation="app", ts_start=end, ts_end=end,
                        measurement="log_record", value=9 if ok else 17, unit="severity", trace_id=trace,
                        span_id=span, status="ok" if ok else "error", severity=9 if ok else 17,
                        text="request completed" if ok else "unhandled exception"))
        return end, ok

    for k in range(int(duration_s / 0.5)):
        t = T0 + int(k * 0.5 * SEC)
        serve("gateway", t, f"{k:032x}", None)
    for k in range(int(duration_s / 5)):
        t = T0 + k * 5 * SEC
        for svc in ("gateway", "order", "inventory", "payment", "postgres"):
            if faulty(svc, t) and fault == "crash":
                continue
            out.append(_rec(modality="metric", source="docker_stats", service=svc, operation="container.cpu.utilization",
                            ts_start=t, ts_end=t, measurement="container_cpu", value=5 + rng.random(), unit="%"))
            if svc != "postgres":
                out.append(_rec(modality="metric", source="otlp", service=svc, operation="process.cpu.utilization",
                                ts_start=t, ts_end=t, measurement="process_cpu", value=0.05, unit="1"))
            if svc in ("order", "inventory"):
                out.append(_rec(modality="metric", source="otlp", service=svc, operation="app.db.pool.checked_out",
                                ts_start=t, ts_end=t, measurement="pool_checked_out",
                                value=5.0 if faulty(svc, t) and fault == "pool" else float(rng.random() < 0.2),
                                unit="{connection}"))
    out.sort(key=lambda r: (r["ts_start"], r["modality"], r["service"], r["measurement"]))
    return assign_ids(out, "00000000-0000-4000-8000-000000000000")


def make_view(fault=None, target=None, seed=0, drop_modalities=(), view_id="vw-synth", **kw):
    recs = [r for r in make_records(fault, target, seed, **kw) if r["modality"] not in drop_modalities]
    return View(view_id, "00000000-0000-4000-8000-000000000000", recs, frozenset(drop_modalities))
