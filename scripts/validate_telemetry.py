"""Phase 1 telemetry validation.

Reads the OTLP-JSON files written by the collector's file exporter (the only
input besides the smoke-test report and the collector's self-metrics), and
checks traces, logs and metrics independently. Every check records evidence
(IDs, counts) so the result can be audited. Nothing here generates telemetry.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

APP_SERVICES = ["gateway", "order", "payment", "inventory"]
CONTAINERS = APP_SERVICES + ["postgres", "otel-collector"]
DB_SERVICES = ["order", "inventory"]
# (caller, callee) pairs that a successful order placement must exercise.
ORDER_EDGES = [("gateway", "inventory"), ("gateway", "order"), ("order", "inventory"), ("order", "payment")]
KIND = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_ERROR = 2
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")
# Clock tolerance between host (smoke client) and Colima VM, and between nested spans.
HOST_VM_SLACK_NS = 500_000_000
NESTING_SLACK_NS = 1_000_000


def attr_value(v: dict):
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [attr_value(x) for x in v["arrayValue"].get("values", [])]
    return v


def attrs(items) -> dict:
    return {a["key"]: attr_value(a["value"]) for a in items or []}


def read_jsonl(path: Path, top_key: str) -> tuple[list, dict]:
    """Parse one exporter file; returns the resource-level entries and file stats."""
    entries, lines, bad = [], 0, 0
    raw = path.read_bytes() if path.exists() else b""
    for line in raw.splitlines():
        if not line.strip():
            continue
        lines += 1
        try:
            entries.extend(json.loads(line)[top_key])
        except (ValueError, KeyError):
            bad += 1
    return entries, {"file": path.name, "exists": path.exists(), "bytes": len(raw), "lines": lines,
                     "unparseable_lines": bad, "sha256": hashlib.sha256(raw).hexdigest()}


def load_spans(entries) -> list[dict]:
    spans = []
    for rs in entries:
        res = attrs(rs.get("resource", {}).get("attributes"))
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                spans.append({
                    "service": res.get("service.name"),
                    "instance": res.get("service.instance.id"),
                    "scope": ss.get("scope", {}).get("name"),
                    "trace_id": s.get("traceId", ""),
                    "span_id": s.get("spanId", ""),
                    "parent_span_id": s.get("parentSpanId", ""),
                    "name": s.get("name"),
                    "kind": KIND.get(s.get("kind", 0), str(s.get("kind"))),
                    "start": int(s.get("startTimeUnixNano", 0)),
                    "end": int(s.get("endTimeUnixNano", 0)),
                    "status": s.get("status", {}).get("code", 0),
                    "attrs": attrs(s.get("attributes")),
                })
    return spans


def load_logs(entries) -> list[dict]:
    logs = []
    for rl in entries:
        res = attrs(rl.get("resource", {}).get("attributes"))
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                body = rec.get("body", {})
                logs.append({
                    "service": res.get("service.name"),
                    "instance": res.get("service.instance.id"),
                    "source": res.get("telemetry.source", "otlp"),
                    "time": int(rec.get("timeUnixNano") or rec.get("observedTimeUnixNano") or 0),
                    "severity": rec.get("severityText"),
                    "severity_number": rec.get("severityNumber", 0),
                    "body": attr_value(body) if body else None,
                    "trace_id": rec.get("traceId", ""),
                    "span_id": rec.get("spanId", ""),
                    "attrs": attrs(rec.get("attributes")),
                })
    return logs


def load_metric_points(entries) -> list[dict]:
    """Flatten every data point; keeps resource identity, metric name/type and point attributes."""
    points = []
    for rm in entries:
        res = attrs(rm.get("resource", {}).get("attributes"))
        identity = res.get("service.name") or res.get("compose.service")
        for sm in rm.get("scopeMetrics", []):
            for m in sm.get("metrics", []):
                for mtype in ("sum", "gauge", "histogram", "exponentialHistogram", "summary"):
                    if mtype in m:
                        break
                else:
                    continue
                for dp in m[mtype].get("dataPoints", []):
                    if mtype == "histogram":
                        value = {"count": int(dp.get("count", 0)), "sum": dp.get("sum")}
                    else:
                        value = float(dp["asDouble"]) if "asDouble" in dp else int(dp.get("asInt", 0))
                    points.append({
                        "identity": identity,
                        "source": "docker_stats" if "compose.service" in res else "otlp",
                        "resource": res,
                        "name": m["name"],
                        "type": mtype,
                        "time": int(dp.get("timeUnixNano", 0)),
                        "start": int(dp.get("startTimeUnixNano", 0)),
                        "attrs": attrs(dp.get("attributes")),
                        "value": value,
                    })
    return points


def latest_cumulative(points, identity, name) -> dict:
    """Last data point per attribute set (cumulative temporality -> final totals)."""
    latest = {}
    for p in points:
        if p["identity"] == identity and p["name"] == name:
            key = tuple(sorted(p["attrs"].items()))
            if key not in latest or p["time"] >= latest[key]["time"]:
                latest[key] = p
    return latest


def parse_prometheus(text: str) -> dict:
    """Sum collector self-metrics by (name, label) for the label that matters (receiver/exporter)."""
    totals = defaultdict(float)
    for line in text.splitlines():
        m = re.match(r"^(otelcol_\w+?)(?:_total)?\{([^}]*)\}\s+([0-9.eE+-]+)", line)
        if not m:
            continue
        name, labels, value = m.group(1), dict(re.findall(r'(\w+)="([^"]*)"', m.group(2))), float(m.group(3))
        component = labels.get("receiver") or labels.get("exporter") or ""
        totals[(name, component)] += value
    return totals


class Report:
    def __init__(self):
        self.checks = []

    def add(self, check_id, signal, description, passed, evidence):
        self.checks.append({"id": check_id, "signal": signal, "description": description,
                            "status": "pass" if passed else "fail", "evidence": evidence})
        print(f"[{'PASS' if passed else 'FAIL'}] {check_id} {description}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telemetry-dir", required=True)
    parser.add_argument("--smoke", required=True, help="smoke_test_results.json")
    parser.add_argument("--collector-metrics", required=True, help="Prometheus text scraped from :8888")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    tdir = Path(args.telemetry_dir)
    smoke = json.loads(Path(args.smoke).read_text())
    span_entries, traces_file = read_jsonl(tdir / "traces.jsonl", "resourceSpans")
    log_entries, logs_file = read_jsonl(tdir / "logs.jsonl", "resourceLogs")
    metric_entries, metrics_file = read_jsonl(tdir / "metrics.jsonl", "resourceMetrics")
    spans, logs, points = load_spans(span_entries), load_logs(log_entries), load_metric_points(metric_entries)
    rep = Report()

    window_lo = smoke["requests"][0]["sent_unix_nano"] - HOST_VM_SLACK_NS
    window_hi = smoke["requests"][-1]["received_unix_nano"] + HOST_VM_SLACK_NS
    smoke_traces = {r["trace_id"]: r for r in smoke["requests"] if r["trace_id"]}
    by_trace = defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(s)
    span_index = {(s["trace_id"], s["span_id"]): s for s in spans}

    # ---------------------------------------------------------------- persistence
    files = [traces_file, logs_file, metrics_file]
    rep.add("P1", "all", "Collector output persisted to local files and read back (every line parses as OTLP JSON)",
            all(f["exists"] and f["bytes"] > 0 and f["unparseable_lines"] == 0 for f in files),
            {"files": files, "records": {"spans": len(spans), "log_records": len(logs),
                                         "metric_points": len(points)}})

    # ---------------------------------------------------------------- traces
    per_service = Counter(s["service"] for s in spans)
    rep.add("T1", "traces", "Spans received from every application service",
            all(per_service[s] > 0 for s in APP_SERVICES), dict(per_service))

    bad_ids = [s for s in spans if not (HEX32.match(s["trace_id"]) and s["trace_id"] != "0" * 32
                                        and HEX16.match(s["span_id"]) and s["span_id"] != "0" * 16
                                        and (s["parent_span_id"] == "" or HEX16.match(s["parent_span_id"])))]
    dup_ids = [k for k, n in Counter((s["trace_id"], s["span_id"]) for s in spans).items() if n > 1]
    rep.add("T2", "traces", "All trace IDs (32 hex) and span IDs (16 hex) are valid, non-zero and unique",
            not bad_ids and not dup_ids,
            {"spans_checked": len(spans), "invalid": [(s["trace_id"], s["span_id"]) for s in bad_ids][:10],
             "duplicates": dup_ids[:10]})

    missing = [t for t in smoke_traces if t not in by_trace]
    rep.add("T3", "traces", "Every trace ID returned to the smoke client is present in the exported traces",
            not missing, {"smoke_trace_ids": list(smoke_traces), "missing": missing})

    tree_problems, tree_evidence = [], {}
    for tid, req in smoke_traces.items():
        tspans = by_trace.get(tid, [])
        ids = {s["span_id"] for s in tspans}
        roots = [s for s in tspans if not s["parent_span_id"]]
        orphans = [s["span_id"] for s in tspans if s["parent_span_id"] and s["parent_span_id"] not in ids]
        ok = (len(roots) == 1 and roots[0]["service"] == "gateway" and roots[0]["kind"] == "SERVER"
              and not orphans)
        if not ok:
            tree_problems.append(tid)
        tree_evidence[tid] = {"label": req["label"], "spans": len(tspans),
                              "root": (roots[0]["service"], roots[0]["kind"], roots[0]["name"]) if roots else None,
                              "roots": len(roots), "orphans": orphans}
    rep.add("T4", "traces", "Each smoke trace is one tree: a single gateway SERVER root, every parent present",
            bool(smoke_traces) and not tree_problems, {"problems": tree_problems, "traces": tree_evidence})

    def cross_service_edges(tspans):
        """(caller, callee) for each CLIENT span whose direct child is a SERVER span in another service."""
        by_id = {s["span_id"]: s for s in tspans}
        edges = []
        for s in tspans:
            parent = by_id.get(s["parent_span_id"])
            if s["kind"] == "SERVER" and parent and parent["kind"] == "CLIENT" and parent["service"] != s["service"]:
                edges.append((parent["service"], s["service"], parent["span_id"], s["span_id"]))
        return edges

    order_traces = [r["trace_id"] for r in smoke["requests"] if r["label"].startswith("order:") and r["ok"]]
    prop_evidence, prop_ok = {}, bool(order_traces)
    for tid in order_traces:
        edges = cross_service_edges(by_trace.get(tid, []))
        pairs = {(a, b) for a, b, _, _ in edges}
        prop_ok &= all(e in pairs for e in ORDER_EDGES)
        prop_evidence[tid] = [{"caller": a, "callee": b, "client_span_id": c, "server_span_id": d}
                              for a, b, c, d in edges]
    rep.add("T5", "traces", "Trace context propagates across service boundaries: every order trace contains "
                            "gateway->inventory, gateway->order, order->inventory and order->payment "
                            "(CLIENT span parent of the callee's SERVER span)",
            prop_ok, {"required_edges": ORDER_EDGES, "traces": prop_evidence})

    db_evidence, db_ok = {}, bool(order_traces)
    for tid in order_traces:
        tspans = by_trace.get(tid, [])
        by_id = {s["span_id"]: s for s in tspans}
        found = {}
        for s in tspans:
            system = s["attrs"].get("db.system.name") or s["attrs"].get("db.system")
            if s["kind"] == "CLIENT" and system == "postgresql":
                parent = by_id.get(s["parent_span_id"])
                found.setdefault(s["service"], []).append(
                    {"span_id": s["span_id"], "name": s["name"],
                     "parent_kind": parent["kind"] if parent else None,
                     "parent_service": parent["service"] if parent else None})
        db_ok &= all(svc in found and all(d["parent_service"] == svc for d in found[svc]) for svc in DB_SERVICES)
        db_evidence[tid] = found
    rep.add("T6", "traces", "Database calls appear as postgresql CLIENT spans inside order and inventory, "
                            "parented within the same service",
            db_ok, db_evidence)

    instances = defaultdict(set)
    for s in spans:
        instances[s["service"]].add(s["instance"])
    server_routes = defaultdict(set)
    for s in spans:
        if s["kind"] == "SERVER":
            server_routes[s["service"]].add(s["attrs"].get("http.route"))
    route_prefix = {"gateway": "/api/", "order": "/orders", "payment": "/charge", "inventory": ("/stock", "/reserve",
                                                                                                 "/release")}
    route_ok = all(all(r and r.startswith(route_prefix[svc]) for r in routes)
                   for svc, routes in server_routes.items() if svc in route_prefix)
    rep.add("T7", "traces", "Service identity is consistent: each service has exactly one instance id, only "
                            "known service names appear, and SERVER spans carry that service's own routes",
            set(per_service) <= set(APP_SERVICES) and all(len(v) == 1 for v in instances.values()) and route_ok,
            {"instances": {k: sorted(v) for k, v in instances.items()},
             "server_routes": {k: sorted(v) for k, v in server_routes.items()}})

    # ---------------------------------------------------------------- logs
    log_services = Counter(l["service"] for l in logs)
    rep.add("L1", "logs", "Log records received from every application service (OTLP) and from postgres "
                          "(Docker fluentd driver)",
            all(log_services[s] > 0 for s in APP_SERVICES + ["postgres"]), dict(log_services))

    app_logs = [l for l in logs if l["source"] == "otlp"]
    unstructured = [l for l in app_logs if not (l["severity"] and isinstance(l["body"], str) and l["time"]
                                                and l["attrs"])]
    pg_logs = [l for l in logs if l["service"] == "postgres"]
    pg_envelope = [l for l in pg_logs if "container_name" in l["attrs"] and l["time"]]
    keys = Counter(k for l in app_logs for k in l["attrs"])
    rep.add("L2", "logs", "Application logs are structured (severity, timestamp, message, typed attributes); "
                          "postgres lines arrive in a structured envelope (container identity, timestamp) "
                          "with a plain-text body",
            bool(app_logs) and not unstructured and len(pg_envelope) == len(pg_logs) > 0,
            {"app_log_records": len(app_logs), "unstructured": len(unstructured),
             "common_attribute_keys": dict(keys.most_common(12)),
             "postgres_records": len(pg_logs), "postgres_with_envelope": len(pg_envelope),
             "postgres_sample": pg_logs[-1]["body"] if pg_logs else None})

    corr_evidence, corr_ok = {}, bool(smoke_traces)
    for tid in smoke_traces:
        tlogs = [l for l in logs if l["trace_id"] == tid]
        trace_services = {s["service"] for s in by_trace.get(tid, [])}
        mismatched = []
        for l in tlogs:
            span = span_index.get((l["trace_id"], l["span_id"]))
            if span is None or span["service"] != l["service"] or not (
                    span["start"] - NESTING_SLACK_NS <= l["time"] <= span["end"] + NESTING_SLACK_NS):
                mismatched.append({"service": l["service"], "span_id": l["span_id"], "body": l["body"]})
        covered = {l["service"] for l in tlogs}
        corr_ok &= bool(tlogs) and not mismatched and trace_services <= covered
        corr_evidence[tid] = {"log_records": len(tlogs), "services_in_trace": sorted(trace_services),
                              "services_with_logs": sorted(covered), "mismatched": mismatched,
                              "sample": [{"service": l["service"], "span_id": l["span_id"], "body": l["body"]}
                                         for l in tlogs[:3]]}
    rep.add("L3", "logs", "Logs correlate to traces: every smoke trace has logs from each service in it, and "
                          "each log's (trace_id, span_id) resolves to a span of the same service whose time "
                          "window contains the log timestamp",
            corr_ok, corr_evidence)

    pg_in_window = [l for l in pg_logs if window_lo - 10**9 <= l["time"] <= window_hi]  # fluentd time: 1 s
    pool_conns = {svc: [l["body"] for l in pg_in_window if "connection authorized" in str(l["body"])
                        and f"application_name={svc}" in str(l["body"])] for svc in DB_SERVICES}
    rep.add("L4", "logs", "Postgres logged the connections opened by the order and inventory pools during the "
                          "smoke window (matched by application_name)",
            all(pool_conns.values()), {"records_in_window": len(pg_in_window),
                                        "pool_connections": {k: v[:2] for k, v in pool_conns.items()}})

    # ---------------------------------------------------------------- metrics
    required = ["app.requests", "app.request.duration", "http.server.request.duration",
                "process.cpu.time", "process.memory.usage", "process.cpu.utilization"]
    names = defaultdict(set)
    for p in points:
        names[p["identity"]].add(p["name"])
    missing_metrics = {s: [m for m in required if m not in names[s]] for s in APP_SERVICES}
    rep.add("M1", "metrics", "Every service exports request count, latency histogram (app and HTTP semconv) "
                             "and process CPU/memory metrics",
            not any(missing_metrics.values()),
            {"missing": missing_metrics, "metric_names": {s: sorted(names[s]) for s in APP_SERVICES}})

    # What each service must have counted, derived from the smoke client's own observations.
    expected = defaultdict(Counter)
    for r in smoke["requests"]:
        expected["gateway"][(r["route"], r["method"], r["status_code"])] += 1
        if r["route"] in ("/api/stock/{item_id}", "/api/orders") and r["status_code"] in (200, 201, 409):
            expected["inventory"][("/stock/{item_id}", "GET", 200)] += 1  # gateway checks stock first
        if r["route"] == "/api/orders" and r["status_code"] == 201:
            expected["order"][("/orders", "POST", 201)] += 1
            expected["inventory"][("/reserve", "POST", 200)] += 1
            expected["payment"][("/charge", "POST", 200)] += 1
        if r["route"] == "/api/orders/{order_id}" and r["status_code"] == 200:
            expected["order"][("/orders/{order_id}", "GET", 200)] += 1
    counts_evidence, counts_ok = {}, True
    for svc in APP_SERVICES:
        observed = Counter()
        for p in latest_cumulative(points, svc, "app.requests").values():
            a = p["attrs"]
            observed[(a.get("http.route"), a.get("http.request.method"), a.get("http.response.status_code"))] += \
                p["value"]
        counts_ok &= observed == expected[svc]
        counts_evidence[svc] = {"expected": {"|".join(map(str, k)): v for k, v in sorted(expected[svc].items())},
                                "observed": {"|".join(map(str, k)): v for k, v in sorted(observed.items())}}
    rep.add("M2", "metrics", "Request counters equal the requests the smoke client actually made, per service, "
                             "route, method and status",
            counts_ok, counts_evidence)

    hist_evidence, hist_ok = {}, True
    for svc in APP_SERVICES:
        req = {k: p["value"] for k, p in latest_cumulative(points, svc, "app.requests").items()}
        hist = {k: p["value"] for k, p in latest_cumulative(points, svc, "app.request.duration").items()}
        semconv = sum(p["value"]["count"] for p in latest_cumulative(points, svc, "http.server.request.duration")
                      .values())
        app_total = sum(v["count"] for v in hist.values())
        ok = (set(req) == set(hist) and all(hist[k]["count"] == req[k] and (hist[k]["sum"] or 0) > 0 for k in req)
              and semconv == app_total)
        hist_ok &= ok
        hist_evidence[svc] = {"app_histogram_count": app_total, "semconv_histogram_count": semconv,
                              "mean_latency_ms": {"|".join(str(v) for _, v in k): round(h["sum"] / h["count"], 3)
                                                  for k, h in hist.items() if h["count"]}}
    rep.add("M3", "metrics", "Latency histograms are consistent: per-series count equals the request counter, "
                             "sums are positive, and the OTel HTTP semconv histogram agrees",
            hist_ok, hist_evidence)

    expected_client_errors = sum(r["expected_error"] for r in smoke["requests"])
    err_points = {svc: latest_cumulative(points, svc, "app.errors") for svc in APP_SERVICES}
    err_totals = {svc: Counter() for svc in APP_SERVICES}
    for svc, pts in err_points.items():
        for p in pts.values():
            err_totals[svc][p["attrs"].get("error.type")] += p["value"]
    server_errors = sum(c["server"] for c in err_totals.values())
    rep.add("M4", "metrics", "Error counters reflect reality: gateway counted exactly the expected client "
                             "rejection(s); no server errors anywhere",
            err_totals["gateway"]["client"] == expected_client_errors and server_errors == 0
            and all(sum(err_totals[s].values()) == 0 for s in APP_SERVICES if s != "gateway"),
            {"expected_client_errors": expected_client_errors,
             "observed": {s: dict(c) for s, c in err_totals.items()}})

    stats = defaultdict(set)
    for p in points:
        if p["source"] == "docker_stats" and window_lo <= p["time"] <= window_hi + 30 * 10**9:
            stats[p["resource"]["compose.service"]].add(p["name"])
    need = {"container.cpu.utilization", "container.memory.usage.total"}
    expected_containers = [c for c in CONTAINERS if c != "otel-collector"]  # collector excluded in its config
    rep.add("M5", "metrics", "Container resource metrics (docker_stats: CPU, memory, network) for every "
                             "service and postgres during the smoke window",
            all(need <= stats[c] for c in expected_containers),
            {c: sorted(stats[c]) for c in expected_containers})

    # ---------------------------------------------------------------- collector
    prom = parse_prometheus(Path(args.collector_metrics).read_text())
    def g(name, comp):
        return prom.get((name, comp), 0.0)
    collector = {
        "otlp_accepted_spans": g("otelcol_receiver_accepted_spans", "otlp"),
        "otlp_refused_spans": g("otelcol_receiver_refused_spans", "otlp"),
        "otlp_accepted_log_records": g("otelcol_receiver_accepted_log_records", "otlp"),
        "otlp_refused_log_records": g("otelcol_receiver_refused_log_records", "otlp"),
        "otlp_accepted_metric_points": g("otelcol_receiver_accepted_metric_points", "otlp"),
        "otlp_refused_metric_points": g("otelcol_receiver_refused_metric_points", "otlp"),
        "fluent_forward_accepted_log_records": g("otelcol_receiver_accepted_log_records", "fluent_forward"),
        "docker_stats_accepted_metric_points": g("otelcol_receiver_accepted_metric_points", "docker_stats"),
        "file_traces_sent_spans": g("otelcol_exporter_sent_spans", "file/traces"),
        "file_logs_sent_log_records": g("otelcol_exporter_sent_log_records", "file/logs"),
        "file_metrics_sent_metric_points": g("otelcol_exporter_sent_metric_points", "file/metrics"),
        "send_failed": sum(v for (n, _), v in prom.items() if n.startswith("otelcol_exporter_send_failed")),
        "refused_total": sum(v for (n, _), v in prom.items() if n.startswith("otelcol_receiver_refused")),
    }
    read_back = {"spans": len(spans), "log_records": len(logs), "metric_points": len(points)}
    sent = {"spans": collector["file_traces_sent_spans"], "log_records": collector["file_logs_sent_log_records"],
            "metric_points": collector["file_metrics_sent_metric_points"]}
    rep.add("P2", "all", "Nothing lost between collector and disk: records read back from the files equal the "
                         "counts the file exporters report as sent",
            all(read_back[k] == sent[k] for k in read_back), {"read_back": read_back, "exporter_sent": sent})
    rep.add("C1", "collector", "Collector self-metrics: OTLP, fluent_forward and docker_stats receivers "
                               "accepted data, file exporters sent it, nothing was refused or failed",
            all(collector[k] > 0 for k in collector if "accepted" in k or "sent" in k)
            and collector["send_failed"] == 0 and collector["refused_total"] == 0,
            collector)

    # ---------------------------------------------------------------- consistency
    bad_order = [s["span_id"] for s in spans if not 0 < s["start"] <= s["end"]]
    outside = [s["span_id"] for tid in smoke_traces for s in by_trace.get(tid, [])
               if not (window_lo <= s["start"] and s["end"] <= window_hi)]
    nesting = []
    for tid in smoke_traces:
        idx = {s["span_id"]: s for s in by_trace.get(tid, [])}
        for s in idx.values():
            p = idx.get(s["parent_span_id"])
            if p and not (p["start"] - NESTING_SLACK_NS <= s["start"] and s["end"] <= p["end"] + NESTING_SLACK_NS):
                nesting.append({"trace_id": tid, "span_id": s["span_id"], "parent": p["span_id"]})
    skew = []
    for tid, r in smoke_traces.items():
        root = next((s for s in by_trace.get(tid, []) if not s["parent_span_id"]), None)
        if root:
            skew.append({"trace_id": tid, "client_send_to_root_start_ms": round((root["start"] - r["sent_unix_nano"]) / 1e6, 3),
                         "root_end_to_client_receive_ms": round((r["received_unix_nano"] - root["end"]) / 1e6, 3)})
    metric_bad_time = [p["name"] for p in points if p["time"] and p["start"] and p["start"] > p["time"]]
    rep.add("X1", "consistency", "Timestamps are consistent: start <= end for all spans, smoke spans fall "
                                 "inside the client's request window (host/VM slack 500 ms), children nest in "
                                 "parents (1 ms slack), metric points have start <= time",
            not bad_order and not outside and not nesting and not metric_bad_time,
            {"bad_order": bad_order[:10], "outside_window": outside[:10], "nesting_violations": nesting[:10],
             "metric_time_violations": metric_bad_time[:10], "client_vs_root_span_offsets": skew})

    log_instances = defaultdict(set)
    for l in app_logs:
        log_instances[l["service"]].add(l["instance"])
    metric_instances = defaultdict(set)
    for p in points:
        if p["source"] == "otlp":
            metric_instances[p["identity"]].add(p["resource"].get("service.instance.id"))
    same = all(instances[s] == log_instances[s] == metric_instances[s] for s in APP_SERVICES)
    rep.add("X2", "consistency", "Service identity is the same across signals: each service reports one "
                                 "service.instance.id, identical in its traces, logs and metrics",
            same and all(len(instances[s]) == 1 for s in APP_SERVICES),
            {s: {"traces": sorted(instances[s]), "logs": sorted(log_instances[s]),
                 "metrics": sorted(metric_instances[s])} for s in APP_SERVICES})

    # ---------------------------------------------------------------- errors
    expected_error_traces = {r["trace_id"] for r in smoke["requests"] if r["expected_error"]}
    error_spans = [s for s in spans if s["status"] == STATUS_ERROR]
    server_5xx = [s for s in spans if s["kind"] == "SERVER" and (s["attrs"].get("http.response.status_code") or 0) >= 500]
    error_logs = [l for l in app_logs if l["severity_number"] >= 17]
    warn_logs = [l for l in app_logs if 13 <= l["severity_number"] < 17]
    unexpected_warn = [l for l in warn_logs if l["trace_id"] not in expected_error_traces]
    rep.add("E1", "all", "No unexpected service errors: no ERROR-status spans, no 5xx server spans, no "
                         "ERROR logs; every WARN log belongs to the deliberately rejected request",
            not error_spans and not server_5xx and not error_logs and not unexpected_warn,
            {"error_spans": [(s["service"], s["name"], s["span_id"]) for s in error_spans],
             "server_5xx": [(s["service"], s["name"]) for s in server_5xx],
             "error_logs": [(l["service"], l["body"]) for l in error_logs],
             "warn_logs": [{"service": l["service"], "trace_id": l["trace_id"], "body": l["body"]} for l in warn_logs],
             "expected_error_trace_ids": sorted(expected_error_traces)})

    by_signal = defaultdict(list)
    for c in rep.checks:
        by_signal[c["signal"]].append(c["status"] == "pass")
    report = {
        "schema": "evidencerca.phase1.telemetry_validation/v1",
        "run_id": smoke["run_id"],
        "inputs": {"telemetry_files": files, "smoke_report": Path(args.smoke).name,
                   "collector_metrics": Path(args.collector_metrics).name},
        "sample_trace_ids": {r["label"]: r["trace_id"] for r in smoke["requests"]},
        "summary": {
            "checks": len(rep.checks),
            "passed": sum(c["status"] == "pass" for c in rep.checks),
            "failed": [c["id"] for c in rep.checks if c["status"] == "fail"],
            "traces_validated": all(by_signal["traces"]),
            "logs_validated": all(by_signal["logs"]),
            "metrics_validated": all(by_signal["metrics"]),
            "collector_validated": all(by_signal["collector"]),
            "all_passed": all(c["status"] == "pass" for c in rep.checks),
        },
        "checks": rep.checks,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str) + "\n")
    s = report["summary"]
    print(f"telemetry validation: {s['passed']}/{s['checks']} checks passed -> {args.out}")
    return 0 if s["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
