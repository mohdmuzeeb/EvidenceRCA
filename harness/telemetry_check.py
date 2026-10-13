"""Per-case telemetry completeness, loss and throughput (harness side; may use ground truth).

Reuses the Phase 1 loaders (scripts/validate_telemetry.py) unchanged.
"""

import re
from collections import defaultdict
from pathlib import Path

from harness.docker_ops import APP_SERVICES
from harness.validate import in_window, windows
from scripts.validate_telemetry import (load_logs, load_metric_points, load_spans, parse_prometheus,
                                        read_jsonl)

SDK_DROP_PATTERNS = re.compile(r"Queue is full|dropp(ed|ing) (span|log|metric)|Failed to export|"
                               r"Transient error .* exporting", re.I)


def load_raw(tel_dir: Path):
    span_entries, f_tr = read_jsonl(tel_dir / "traces.jsonl", "resourceSpans")
    log_entries, f_lg = read_jsonl(tel_dir / "logs.jsonl", "resourceLogs")
    metric_entries, f_mt = read_jsonl(tel_dir / "metrics.jsonl", "resourceMetrics")
    return load_spans(span_entries), load_logs(log_entries), load_metric_points(metric_entries), [f_tr, f_lg, f_mt]


def check_case(gt: dict, tel_dir: Path, collector_prom: str, requests: list[dict], container_stdout: str,
               spans=None, logs=None, points=None, files=None) -> dict:
    if spans is None:
        spans, logs, points, files = load_raw(tel_dir)
    tl = gt["timeline_vm_ns"]
    win = windows(tl)
    prom = parse_prometheus(collector_prom)

    def g(name, comp):
        return prom.get((name, comp), 0.0)

    sent = {"spans": g("otelcol_exporter_sent_spans", "file/traces"),
            "log_records": g("otelcol_exporter_sent_log_records", "file/logs"),
            "metric_points": g("otelcol_exporter_sent_metric_points", "file/metrics")}
    read_back = {"spans": len(spans), "log_records": len(logs), "metric_points": len(points)}
    refused = sum(v for (n, _), v in prom.items() if n.startswith("otelcol_receiver_refused"))
    failed = sum(v for (n, _), v in prom.items() if n.startswith("otelcol_exporter_send_failed"))
    accepted = {"otlp_spans": g("otelcol_receiver_accepted_spans", "otlp"),
                "otlp_log_records": g("otelcol_receiver_accepted_log_records", "otlp"),
                "otlp_metric_points": g("otelcol_receiver_accepted_metric_points", "otlp"),
                "fluent_forward_log_records": g("otelcol_receiver_accepted_log_records", "fluent_forward"),
                "docker_stats_metric_points": g("otelcol_receiver_accepted_metric_points", "docker_stats")}

    # Trace completeness: every response that carried a trace id must have its gateway root span on disk.
    roots = {s["trace_id"] for s in spans if s["service"] == "gateway" and s["kind"] == "SERVER"
             and not s["parent_span_id"]}
    responded = [r for r in requests if r.get("trace_id")]
    missing = [r["trace_id"] for r in responded if r["trace_id"] not in roots]
    no_response = [r for r in requests if not r.get("shed") and r.get("status") is None]

    # Per-window presence of each modality per service.
    span_t = defaultdict(list)
    for s in spans:
        span_t[s["service"]].append(s["start"])
    log_t = defaultdict(list)
    for l in logs:
        log_t[l["service"]].append(l["time"])
    met_t, ds_t = defaultdict(list), defaultdict(list)
    for p in points:
        (ds_t if p["source"] == "docker_stats" else met_t)[p["identity"]].append(p["time"])
    presence = {}
    for wname in ("baseline", "injection_steady", "recovery_steady"):
        w = win[wname]
        presence[wname] = {
            svc: {"spans": sum(in_window(t, w) for t in span_t[svc]),
                  "logs": sum(in_window(t, w) for t in log_t[svc]),
                  "metrics": sum(in_window(t, w) for t in met_t[svc]),
                  "container_metrics": sum(in_window(t, w) for t in ds_t[svc])}
            for svc in APP_SERVICES}
        presence[wname]["postgres"] = {"logs": sum(in_window(t, w) for t in log_t["postgres"]),
                                       "container_metrics": sum(in_window(t, w) for t in ds_t["postgres"])}

    expected_gap = gt["target"] if gt["fault_type"] == "service_crash" else None

    # Spans/logs are request-driven. A span/log gap is explained only if no inbound call *reached* the service
    # in that window: every caller CLIENT span targeting it (server.address) either failed before a request
    # could arrive (connect-level error) or has its SERVER child outside the window. Periodic metrics and
    # container metrics have no such excuse. Separately, any inbound call that received an HTTP response must
    # have its SERVER child somewhere in the data, otherwise that is telemetry loss.
    server_by_parent = {s["parent_span_id"]: s for s in spans if s["kind"] == "SERVER" and s["parent_span_id"]}
    inbound = {w: defaultdict(lambda: {"calls": 0, "reached_in_window": 0, "not_reached": 0}) for w in presence}
    orphan_responses = []
    for s in spans:
        callee = s["attrs"].get("server.address")
        if s["kind"] != "CLIENT" or callee not in APP_SERVICES:
            continue
        child = server_by_parent.get(s["span_id"])
        if child is None and s["attrs"].get("http.response.status_code") is not None:
            orphan_responses.append({"caller": s["service"], "callee": callee, "span_id": s["span_id"],
                                     "end_ns": s["end"]})
        for wname in presence:
            if in_window(s["start"], win[wname]):
                b = inbound[wname][callee]
                b["calls"] += 1
                if child is not None and in_window(child["start"], win[wname]):
                    b["reached_in_window"] += 1
                elif child is None and s["attrs"].get("http.response.status_code") is None:
                    b["not_reached"] += 1  # connect-level failure: no request arrived
    gaps, explained = [], []
    for wname, svcs in presence.items():
        for svc, mods in svcs.items():
            for mod, n in mods.items():
                if n:
                    continue
                gap = {"window": wname, "service": svc, "modality": mod}
                b = inbound[wname][svc]
                if svc == expected_gap and wname == "injection_steady":
                    explained.append({**gap, "reason": "target container down"})
                elif mod in ("spans", "logs") and svc != "gateway" and b["reached_in_window"] == 0:
                    explained.append({**gap, "reason": f"no inbound request reached the service "
                                                       f"({b['calls']} calls, {b['not_reached']} failed at connect)"})
                else:
                    gaps.append(gap)

    # A SIGKILLed process loses spans still buffered in its batch processor (export interval 1 s). Those
    # answered calls ended just before the kill; they are loss caused by the fault, not by the pipeline.
    kill_buffer_loss = [o for o in orphan_responses if gt["fault_type"] == "service_crash"
                        and o["callee"] == gt["target"]
                        and tl["injection_start_ns"] - 2 * 10**9 <= o["end_ns"] <= tl["injection_start_ns"]]
    pipeline_orphans = [o for o in orphan_responses if o not in kill_buffer_loss]

    duration_s = (tl["recovery_end_ns"] - tl["workload_t0_ns"]) / 1e9
    sdk_drop_lines = [l for l in container_stdout.splitlines() if SDK_DROP_PATTERNS.search(l)]
    checks = {
        "collector_to_disk_lossless": read_back == {k: int(v) for k, v in sent.items()},
        "collector_refused_or_failed_zero": refused == 0 and failed == 0,
        "every_responded_request_has_its_trace": not missing,
        "every_answered_inbound_call_has_its_server_span": not pipeline_orphans,
        "all_modalities_present_per_service_per_window": not gaps,
        "no_sdk_export_drops_logged": not sdk_drop_lines,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "records": read_back, "exporter_sent": sent, "receiver_accepted": accepted,
        "refused": refused, "send_failed": failed,
        "trace_completeness": {"responses_with_trace_id": len(responded), "missing_root_spans": len(missing),
                               "missing_sample": missing[:5],
                               "requests_without_response": len(no_response)},
        "presence_by_window": presence, "unexpected_gaps": gaps, "explained_gaps": explained,
        "inbound_calls_by_window": {w: {k: dict(c) for k, c in v.items()} for w, v in inbound.items()},
        "answered_calls_without_server_span": {"pipeline_loss": pipeline_orphans[:5],
                                               "lost_in_killed_process_buffer": len(kill_buffer_loss)}, "expected_gap": expected_gap,
        "throughput_per_s": {"spans": round(len(spans) / duration_s, 2),
                             "log_records": round(len(logs) / duration_s, 2),
                             "metric_points": round(len(points) / duration_s, 2)},
        "files": files, "raw_bytes": sum(f["bytes"] for f in files),
        "sdk_drop_lines": sdk_drop_lines[:5],
    }
