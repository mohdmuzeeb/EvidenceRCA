"""Independent ground-truth validation, recovery verification and impact measurement.

A fault counts as *occurred* only if its signature is observed through a
mechanism different from the one used to inject it (see docs/fault_catalog.md):

  fault                 injected via                  independent signature (primary)
  service_crash         docker kill                   TCP connect to the service fails (workload container probes)
                                                      + Docker state not running
  processing_latency    control channel delay         client-side latency of requests through the target
                                                      operation rises by >= 80 % of the delay (workload records)
  network_timeout       tc netem in the target netns  TCP connect time to the target >= 80 % of the delay
  db_pool_exhaustion    control channel hold          Postgres sees pool_size connections of the target
                                                      `idle in transaction` (pg_stat_activity)
  cpu_pressure          docker exec busy loops        cgroup CPU usage of the target >= 0.8 cores (cpu.stat
                                                      read on the VM host)
  cascading_failure     psql table lock               Postgres lock table: ACCESS EXCLUSIVE on `items` held by
                                                      a non-service session while service sessions wait

Every detector is evaluated for every service in every case (including the
control), so the result also shows that nothing was present before injection
and that nothing remained after recovery.
"""

import statistics
from collections import defaultdict

from harness.docker_ops import APP_SERVICES
from harness.faults import NETEM_DELAY_MS, POOL_SIZE, PROCESSING_DELAY_MS

SETTLE_NS = 15 * 10**9          # ignore the first 15 s of injection and of recovery when judging steady state
PRESENT_FRACTION = 0.8          # share of injection samples that must show the signature
CPU_CORES_THRESHOLD = 0.8
APP_DB_SERVICES = {"order", "inventory"}

# Workload request kinds whose call path goes through each (service, operation).
KINDS_THROUGH = {
    ("gateway", "POST /api/orders"): ["order"],
    ("order", "POST /orders"): ["order"],
    ("payment", "POST /charge"): ["order"],
    ("inventory", "POST /reserve"): ["order"],
}


def windows(tl: dict) -> dict:
    """VM-clock windows; *_steady excludes the first SETTLE_NS after a transition."""
    return {
        "baseline": (tl["warmup_end_ns"], tl["injection_start_ns"]),
        "injection": (tl["injection_start_ns"], tl["injection_end_ns"]),
        "injection_steady": (tl["injection_start_ns"] + SETTLE_NS, tl["injection_end_ns"]),
        "recovery": (tl["injection_end_ns"], tl["recovery_end_ns"]),
        "recovery_steady": (tl["injection_end_ns"] + SETTLE_NS, tl["recovery_end_ns"]),
        "recovery_late": (tl["recovery_end_ns"] - 30 * 10**9, tl["recovery_end_ns"]),
    }


def in_window(t: int, w: tuple[int, int]) -> bool:
    return w[0] <= t < w[1]


def q(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


# ---------------------------------------------------------------- per-sample signatures
def sample_signatures(monitor: list[dict], probes: list[dict], offset_ns: int) -> list[dict]:
    """Flatten every observation into (t_vm_ns, detector, service, present, value)."""
    out = []
    prev_cg = {}
    for rec in monitor:
        kind = rec["kind"]
        if kind == "docker":
            t = rec["host_ns"] + offset_ns
            for svc, st in rec["state"].items():
                out.append({"t": t, "detector": "container_not_running", "service": svc,
                            "present": not st["running"], "value": st["status"]})
        elif kind == "vm":
            t = rec["vm_ns"]
            for svc, q_ in rec["qdisc"].items():
                out.append({"t": t, "detector": "qdisc_netem", "service": svc, "present": "netem" in q_,
                            "value": q_})
            for svc, cg in rec["cgroup"].items():
                if "usage_usec" in cg and svc in prev_cg:
                    t_prev, cg_prev = prev_cg[svc]
                    dt_us = (t - t_prev) / 1000
                    cores = (cg["usage_usec"] - cg_prev["usage_usec"]) / dt_us if dt_us > 0 else 0.0
                    if cores >= 0:  # a restarted container resets its counters
                        out.append({"t": t, "detector": "cpu_saturated", "service": svc,
                                    "present": cores >= CPU_CORES_THRESHOLD, "value": round(cores, 3),
                                    "throttled": cg.get("nr_throttled", 0) - cg_prev.get("nr_throttled", 0)})
                if "usage_usec" in cg:
                    prev_cg[svc] = (t, cg)
        elif kind == "pg" and "vm_ns" in rec:
            t = rec["vm_ns"]
            idle_tx = defaultdict(int)
            for a in rec["activity"]:
                if a["state"] == "idle in transaction":
                    idle_tx[a["app"]] += a["n"]
            for svc in APP_DB_SERVICES:
                out.append({"t": t, "detector": "pool_held_idle_in_tx", "service": svc,
                            "present": idle_tx[svc] >= POOL_SIZE, "value": idle_tx[svc]})
            foreign_x = sum(l["n"] for l in rec["locks"] if l["granted"] and l["mode"] == "AccessExclusiveLock"
                            and l["table"] == "items" and l["app"] not in APP_SERVICES)
            waiting = sum(l["n"] for l in rec["locks"] if not l["granted"] and l["app"] in APP_SERVICES)
            out.append({"t": t, "detector": "table_lock_blocking", "service": "postgres",
                        "present": foreign_x > 0 and waiting > 0, "value": {"exclusive": foreign_x,
                                                                             "waiting": waiting}})
    for p in probes:
        out.append({"t": p["t_ns"], "detector": "tcp_unreachable", "service": p["target"], "present": not p["ok"],
                    "value": p.get("error")})
        if p["ok"]:
            out.append({"t": p["t_ns"], "detector": "tcp_connect_slow", "service": p["target"],
                        "present": p["connect_ms"] >= 0.8 * NETEM_DELAY_MS, "value": p["connect_ms"]})
    return out


def detector_matrix(sigs: list[dict], win: dict) -> dict:
    """detector -> service -> window -> {n, present_fraction}."""
    acc = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for s in sigs:
        for wname in ("baseline", "injection_steady", "recovery_steady"):
            if in_window(s["t"], win[wname]):
                acc[s["detector"]][s["service"]][wname].append(s["present"])
    return {det: {svc: {w: {"n": len(v), "present_fraction": round(sum(v) / len(v), 3) if v else None}
                        for w, v in ws.items()}
                  for svc, ws in svcs.items()}
            for det, svcs in acc.items()}


# ---------------------------------------------------------------- workload-based measures
def workload_window_stats(requests: list[dict], w: tuple[int, int], kinds: list[str] | None = None) -> dict:
    rs = [r for r in requests if not r.get("shed") and in_window(r["sent_ns"], w)
          and (kinds is None or r["kind"] in kinds)]
    shed = [r for r in requests if r.get("shed") and in_window(r["sched_ns"], w)]
    lat = [r["latency_ms"] for r in rs if r.get("status") is not None]
    dur_s = (w[1] - w[0]) / 1e9
    return {"n": len(rs), "shed": len(shed), "throughput_rps": round(len(rs) / dur_s, 3) if dur_s > 0 else None,
            "unexpected_rate": round(sum(not r["ok"] for r in rs) / len(rs), 4) if rs else None,
            "transport_errors": sum(r.get("status") is None for r in rs),
            "status_5xx": sum((r.get("status") or 0) >= 500 for r in rs),
            "p50_ms": q(lat, 0.5), "p95_ms": q(lat, 0.95)}


def latency_shift(requests, win, kinds) -> dict:
    def med(wname):
        lat = [r["latency_ms"] for r in requests if not r.get("shed") and r["kind"] in kinds and r["ok"]
               and in_window(r["sent_ns"], win[wname])]
        return statistics.median(lat) if lat else None
    b, i, r = med("baseline"), med("injection_steady"), med("recovery_steady")
    return {"baseline_median_ms": b, "injection_median_ms": i, "recovery_median_ms": r,
            "injection_shift_ms": round(i - b, 3) if None not in (i, b) else None,
            "recovery_shift_ms": round(r - b, 3) if None not in (r, b) else None}


def span_latency_shift(spans, win, service, operation) -> dict:
    method, path = operation.split(" ", 1)
    def med(wname):
        d_ = [(s["end"] - s["start"]) / 1e6 for s in spans if s["service"] == service and s["kind"] == "SERVER"
              and s["attrs"].get("http.route") == path and s["attrs"].get("http.request.method") == method
              and in_window(s["start"], win[wname])]
        return statistics.median(d_) if d_ else None
    b, i, r = med("baseline"), med("injection_steady"), med("recovery_steady")
    return {"baseline_median_ms": b, "injection_median_ms": i, "recovery_median_ms": r,
            "injection_shift_ms": round(i - b, 3) if None not in (i, b) else None}


# ---------------------------------------------------------------- fault verdicts
PRIMARY = {
    "service_crash": ("tcp_unreachable", "container_not_running"),
    "network_timeout": ("tcp_connect_slow", "qdisc_netem"),
    "db_pool_exhaustion": ("pool_held_idle_in_tx", None),
    "cpu_pressure": ("cpu_saturated", None),
    "cascading_failure": ("table_lock_blocking", None),
}


def presence_verdict(matrix, detector, service) -> dict:
    cell = matrix.get(detector, {}).get(service, {})
    def frac(w):
        return (cell.get(w) or {}).get("present_fraction")
    b, i, r = frac("baseline"), frac("injection_steady"), frac("recovery_steady")
    return {"detector": detector, "service": service, "baseline_fraction": b, "injection_fraction": i,
            "recovery_fraction": r,
            "absent_before": b == 0.0, "present_during": i is not None and i >= PRESENT_FRACTION,
            "absent_after": r == 0.0}


def validate_case(gt: dict, monitor, probes, requests, spans) -> dict:
    tl = gt["timeline_vm_ns"]
    win = windows(tl)
    offsets = [m["clock_offset_ns"] for m in monitor if m["kind"] == "vm"]
    offset = int(statistics.median(offsets)) if offsets else 0
    sigs = sample_signatures(monitor, probes, offset)
    matrix = detector_matrix(sigs, win)
    fault, target = gt["fault_type"], gt["target"]

    checks = []
    if fault == "processing_latency":
        op = gt["root_operation"]
        kinds = KINDS_THROUGH[(target, op)]
        client = latency_shift(requests, win, kinds)
        server = span_latency_shift(spans, win, target, op)
        need = 0.8 * PROCESSING_DELAY_MS
        checks.append({"check": "client latency through target operation rose", "independent": True,
                       "passed": (client["injection_shift_ms"] or 0) >= need, "evidence": client,
                       "threshold_ms": need})
        checks.append({"check": "target SERVER span duration rose (raw telemetry)", "independent": True,
                       "passed": (server["injection_shift_ms"] or 0) >= need, "evidence": server,
                       "threshold_ms": need})
        checks.append({"check": "client latency back to baseline after cleanup", "independent": True,
                       "passed": client["recovery_shift_ms"] is not None and client["recovery_shift_ms"] < 0.2 * need,
                       "evidence": client})
    elif fault in PRIMARY:
        det, second = PRIMARY[fault]
        svc = "postgres" if fault == "cascading_failure" else target
        v = presence_verdict(matrix, det, svc)
        checks.append({"check": f"{det} present during injection, absent before and after", "independent": True,
                       "passed": v["absent_before"] and v["present_during"] and v["absent_after"], "evidence": v})
        if second:
            v2 = presence_verdict(matrix, second, svc)
            checks.append({"check": f"{second} present during injection, absent before and after",
                           "independent": second != "qdisc_netem",  # qdisc readback uses the injecting tool
                           "passed": v2["absent_before"] and v2["present_during"] and v2["absent_after"],
                           "evidence": v2})

    # Any detector firing anywhere: required to be none for the control; reported for faults.
    firing = []
    for det, svcs in matrix.items():
        for svc, ws in svcs.items():
            for w, cell in ws.items():
                if cell["present_fraction"]:
                    firing.append({"detector": det, "service": svc, "window": w, **cell})
    if fault == "none":
        checks.append({"check": "control: no fault signature in any window", "independent": True,
                       "passed": not firing, "evidence": firing})
    before = [f for f in firing if f["window"] == "baseline"]
    after = [f for f in firing if f["window"] == "recovery_steady"]
    checks.append({"check": "no fault signature of any kind before injection", "independent": True,
                   "passed": not before, "evidence": before})
    checks.append({"check": "no fault signature of any kind after recovery settles", "independent": True,
                   "passed": not after, "evidence": after})

    occurred = all(c["passed"] for c in checks if c["independent"]) if fault != "none" else None
    return {"fault_occurred": occurred, "checks": checks, "detector_matrix": matrix,
            "signatures_during_injection": [f for f in firing if f["window"] == "injection_steady"],
            "clock_offset_ns_median": offset, "monitor_samples": len(monitor), "probe_samples": len(probes)}


def impact_and_recovery(gt: dict, requests, monitor) -> dict:
    win = windows(gt["timeline_vm_ns"])
    base = workload_window_stats(requests, win["baseline"])
    inj = workload_window_stats(requests, win["injection_steady"])
    late = workload_window_stats(requests, win["recovery_late"])
    impact = (inj["unexpected_rate"] or 0) - (base["unexpected_rate"] or 0) >= 0.05 or \
        (inj["p95_ms"] or 0) >= 2 * (base["p95_ms"] or 1e9)
    last_docker = [m for m in monitor if m["kind"] == "docker"][-1]["state"] if monitor else {}
    # Injector-state readback (control channel + busy processes) in every VM sample after recovery settles.
    readbacks = [m for m in monitor if m["kind"] == "vm" and in_window(m["vm_ns"], win["recovery_steady"])]
    clean_readback = bool(readbacks) and all(
        len(m.get("control", {})) == len(APP_SERVICES)
        and all(not c.get("error") and not c.get("delays") and c.get("held_connections") == 0
                for c in m["control"].values())
        and not any(m.get("busy_processes", {}).values())
        for m in readbacks)
    rec_checks = {
        "all_containers_running_and_healthy_at_end": all(
            st["running"] and st["health"] in ("healthy", None) for st in last_docker.values()),
        "injector_state_cleared (readback, every sample after settle)": clean_readback,
        "error_rate_back_to_baseline (last 30 s)": late["unexpected_rate"] is not None and
            late["unexpected_rate"] <= (base["unexpected_rate"] or 0) + 0.02,
        "p95_back_to_baseline (last 30 s)": late["p95_ms"] is not None and base["p95_ms"] is not None and
            late["p95_ms"] <= 2 * base["p95_ms"] + 50,
    }
    return {"observable_impact": {"gateway_slo_deviated": bool(impact), "baseline": base,
                                  "injection_steady": inj, "rule": "unexpected-rate +5 pp or p95 x2"},
            "recovery": {"recovered": all(rec_checks.values()), "checks": rec_checks, "recovery_late": late}}


def propagated_symptoms(spans, win, roots) -> dict:
    """Services (other than the roots) whose SERVER spans deviated during injection. Analysis only."""
    out = {}
    for svc in APP_SERVICES:
        def stats(wname):
            ss = [s for s in spans if s["service"] == svc and s["kind"] == "SERVER" and in_window(s["start"], win[wname])]
            dur = [(s["end"] - s["start"]) / 1e6 for s in ss]
            err = sum((s["attrs"].get("http.response.status_code") or 0) >= 500 or s["status"] == 2 for s in ss)
            return {"n": len(ss), "p95_ms": q(dur, 0.95), "error_rate": round(err / len(ss), 4) if ss else None}
        b, i = stats("baseline"), stats("injection_steady")
        deviated = i["n"] == 0 and b["n"] > 0 or (
            i["n"] > 0 and b["n"] > 0 and ((i["p95_ms"] or 0) >= 1.5 * (b["p95_ms"] or 1e9)
                                            or (i["error_rate"] or 0) - (b["error_rate"] or 0) >= 0.05))
        out[svc] = {"baseline": b, "injection_steady": i, "deviated": bool(deviated)}
    return {"per_service": out, "propagated_symptom_services": sorted(
        s for s, v in out.items() if v["deviated"] and s not in roots)}


def capture_continuity(monitor: list[dict], workload_summary: dict | None, workload_duration_s: float,
                       interval_s: float = 5.0) -> dict:
    """Detect a suspended or stalled capture (e.g. macOS sleep pausing the VM).

    Fails if consecutive VM monitor samples are more than 3 intervals apart, or the workload finished more
    than 10 s later than its planned duration.
    """
    ts = sorted(m["vm_ns"] for m in monitor if m["kind"] == "vm")
    gaps = [(b - a) / 1e9 for a, b in zip(ts, ts[1:])]
    max_gap = max(gaps) if gaps else None
    overrun = None
    if workload_summary:
        overrun = (workload_summary["end_ns"] - workload_summary["t0_ns"]) / 1e9 - workload_duration_s
    ok = max_gap is not None and max_gap <= 3 * interval_s and overrun is not None and overrun <= 10
    return {"passed": ok, "vm_samples": len(ts), "max_sample_gap_s": round(max_gap, 2) if max_gap else None,
            "workload_overrun_s": round(overrun, 2) if overrun is not None else None,
            "rule": "max monitor gap <= 3 x interval and workload overrun <= 10 s"}
