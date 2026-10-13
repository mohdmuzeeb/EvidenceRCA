"""Evidence records and evidence views (agent side).

Raw OTLP documents (from evidencerca.rca_input) are normalized into flat evidence
records. One record per span, per log record, and per metric *interval* (counters and
histograms become rates/means between consecutive exports).

Record fields (docs/experiment_protocol.md §4.3, docs/evidence_verifier_spec.md):
  evidence_id      content-derived hash ("ev-" + 20 hex); deterministic and label-free
  case_id          the opaque case id
  modality         "trace" | "log" | "metric"
  source           "otlp" | "docker_stats" | "fluentd"
  service          emitting service (postgres for its logs/container metrics)
  operation        span name / route, log logger, or metric name
  ts_start, ts_end UTC ns (VM clock); equal for logs
  measurement      span_duration | log_record | request_rate | error_ratio | latency_mean |
                   process_cpu | process_memory | pool_checked_out | container_cpu | container_memory
  value, unit      numeric value and its unit (ms, 1/s, 1, %, By, {connection}, severity)
  trace_id, span_id, parent_span_id   for spans and correlated logs
  kind             SERVER | CLIENT | INTERNAL (spans) or None
  peer_service     callee for CLIENT spans (server.address, or "postgres" for DB statement spans;
                   None for DB connection acquisition, which is local to the caller's pool)
  status           "ok" | "error"
  http_status, error_type, severity, text   optional details

Records carry no provenance pointer into raw files (that map stays with the
evaluation harness), so perturbed or synthetic records cannot be told apart by it.
"""

import hashlib
import json
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

KIND = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
APP_SERVICES = ("gateway", "order", "payment", "inventory")
ALL_SERVICES = APP_SERVICES + ("postgres",)
RECORD_KEYS = ("evidence_id", "case_id", "modality", "source", "service", "operation", "ts_start", "ts_end",
               "measurement", "value", "unit", "trace_id", "span_id", "parent_span_id", "kind", "peer_service",
               "status", "http_status", "error_type", "severity", "text")
PG_LEVEL = {"DEBUG": 5, "LOG": 9, "INFO": 9, "NOTICE": 10, "WARNING": 13, "ERROR": 17, "FATAL": 21, "PANIC": 24}
_PG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) UTC")
_PG_LVL = re.compile(r"\b(DEBUG\d?|LOG|INFO|NOTICE|WARNING|ERROR|FATAL|PANIC):\s")


def _val(v: dict):
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    return None


def _attrs(items) -> dict:
    return {a["key"]: _val(a["value"]) for a in items or []}


def content_id(rec: dict, salt: int = 0) -> str:
    body = {k: rec.get(k) for k in RECORD_KEYS if k not in ("evidence_id", "case_id")}
    if isinstance(body.get("value"), float):
        body["value"] = round(body["value"], 9)
    h = hashlib.sha256((json.dumps(body, sort_keys=True, default=str) + f"#{salt}").encode()).hexdigest()
    return "ev-" + h[:20]


def assign_ids(records: list[dict], case_id: str) -> list[dict]:
    """Content-derived ids; identical records get an occurrence counter so ids stay unique."""
    seen = defaultdict(int)
    for r in records:
        r["case_id"] = case_id
        base = content_id(r)
        k = seen[base]
        seen[base] += 1
        r["evidence_id"] = base if k == 0 else content_id(r, k)
    return records


def _blank(**kw) -> dict:
    rec = dict.fromkeys(RECORD_KEYS)
    rec.update(kw)
    return rec


# ---------------------------------------------------------------- normalization
def spans_to_records(docs) -> list[dict]:
    out = []
    for doc in docs:
        for rs in doc.get("resourceSpans", []):
            svc = _attrs(rs.get("resource", {}).get("attributes")).get("service.name")
            for ss in rs.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    a = _attrs(s.get("attributes"))
                    kind = KIND.get(s.get("kind", 0))
                    start, end = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
                    db = a.get("db.system.name") or a.get("db.system")
                    # DB spans go to postgres, except connection acquisition ("connect"): it includes the wait
                    # for a free pooled connection and never reaches the server when the pool is exhausted,
                    # so it is a local operation of the calling service (signal `pool_wait`).
                    if db == "postgresql":
                        peer = None if s.get("name") == "connect" else "postgres"
                    else:
                        peer = a.get("server.address") if kind == "CLIENT" else None
                    http = a.get("http.response.status_code")
                    err_type = a.get("error.type")
                    if err_type is None:
                        for ev in s.get("events", []):
                            if ev.get("name") == "exception":
                                err_type = _attrs(ev.get("attributes")).get("exception.type")
                                break
                    is_err = s.get("status", {}).get("code") == 2 or (http is not None and http >= 500) or \
                        (kind == "CLIENT" and err_type is not None)
                    out.append(_blank(
                        modality="trace", source="otlp", service=svc,
                        operation=a.get("http.route") or s.get("name"), ts_start=start, ts_end=end,
                        measurement="span_duration", value=round((end - start) / 1e6, 6), unit="ms",
                        trace_id=s.get("traceId"), span_id=s.get("spanId"),
                        parent_span_id=s.get("parentSpanId") or None, kind=kind, peer_service=peer,
                        status="error" if is_err else "ok", http_status=http, error_type=err_type))
    return out


def logs_to_records(docs) -> list[dict]:
    out = []
    for doc in docs:
        for rl in doc.get("resourceLogs", []):
            res = _attrs(rl.get("resource", {}).get("attributes"))
            svc = res.get("service.name")
            fluent = res.get("telemetry.source") == "docker-fluentd"
            for sl in rl.get("scopeLogs", []):
                for r in sl.get("logRecords", []):
                    body = r.get("body", {})
                    text = _val(body) if body else None
                    text = text if isinstance(text, str) else json.dumps(text)
                    ts = int(r.get("timeUnixNano") or r.get("observedTimeUnixNano") or 0)
                    sev = r.get("severityNumber") or 0
                    a = _attrs(r.get("attributes"))
                    if fluent:  # Postgres text: ms timestamp and level live in the message
                        m = _PG_TS.match(text or "")
                        if m:
                            ts = int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
                                     .replace(tzinfo=timezone.utc).timestamp() * 1e9)
                        lv = _PG_LVL.search(text or "")
                        sev = PG_LEVEL.get(re.sub(r"\d", "", lv.group(1)), 9) if lv else 9
                    out.append(_blank(
                        modality="log", source="fluentd" if fluent else "otlp", service=svc,
                        operation=sl.get("scope", {}).get("name") or ("postgres" if fluent else None),
                        ts_start=ts, ts_end=ts, measurement="log_record", value=sev, unit="severity",
                        trace_id=r.get("traceId") or None, span_id=r.get("spanId") or None,
                        status="error" if sev >= 17 else "ok", severity=sev, text=text,
                        error_type=a.get("exception_type"), http_status=a.get("status_code")))
    return out


GAUGES = {"process.cpu.utilization": ("process_cpu", "1"), "process.memory.usage": ("process_memory", "By"),
          "app.db.pool.checked_out": ("pool_checked_out", "{connection}"),
          "container.cpu.utilization": ("container_cpu", "%"),
          "container.memory.usage.total": ("container_memory", "By")}


def metrics_to_records(docs) -> list[dict]:
    """Gauges -> one record per point. app.requests/app.errors -> request_rate and error_ratio per interval;
    app.request.duration -> latency_mean per interval (cumulative deltas, per series start time)."""
    gauges, cum = [], defaultdict(list)
    for doc in docs:
        for rm in doc.get("resourceMetrics", []):
            res = _attrs(rm.get("resource", {}).get("attributes"))
            svc = res.get("service.name") or res.get("compose.service")
            source = "docker_stats" if "compose.service" in res else "otlp"
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    name = m["name"]
                    if name in GAUGES:
                        meas, unit = GAUGES[name]
                        typ = "gauge" if "gauge" in m else "sum"
                        for dp in m.get(typ, {}).get("dataPoints", []):
                            v = float(dp["asDouble"]) if "asDouble" in dp else float(dp.get("asInt", 0))
                            t = int(dp["timeUnixNano"])
                            gauges.append(_blank(modality="metric", source=source, service=svc, operation=name,
                                                 ts_start=t, ts_end=t, measurement=meas, value=v, unit=unit))
                    elif name in ("app.requests", "app.errors"):
                        for dp in m["sum"]["dataPoints"]:
                            cum[(svc, name, int(dp.get("startTimeUnixNano", 0)))].append(
                                (int(dp["timeUnixNano"]), json.dumps(_attrs(dp.get("attributes")), sort_keys=True),
                                 float(dp.get("asInt", dp.get("asDouble", 0)))))
                    elif name == "app.request.duration":
                        for dp in m["histogram"]["dataPoints"]:
                            cum[(svc, name, int(dp.get("startTimeUnixNano", 0)))].append(
                                (int(dp["timeUnixNano"]), json.dumps(_attrs(dp.get("attributes")), sort_keys=True),
                                 (float(dp.get("sum", 0)), int(dp.get("count", 0)))))
    out = list(gauges)
    # Per-series deltas between consecutive exports (a series' first point counts from its own start time),
    # then summed over all attribute series of the service at each export time (request-weighted).
    agg = defaultdict(lambda: {"t0": None, "req": 0.0, "err": 0.0, "dsum": 0.0, "dcount": 0.0})
    for (svc, name, start), pts in cum.items():
        series = defaultdict(list)
        for t, key, v in pts:
            series[key].append((t, v))
        for key, sp in series.items():
            sp.sort()
            prev_t, prev_v = start, (0.0, 0) if name == "app.request.duration" else 0.0
            for t, v in sp:
                a = agg[(svc, t)]
                a["t0"] = prev_t if a["t0"] is None else max(a["t0"], prev_t)
                if name == "app.request.duration":
                    a["dsum"] += v[0] - prev_v[0]
                    a["dcount"] += v[1] - prev_v[1]
                elif name == "app.requests":
                    a["req"] += v - prev_v
                else:
                    a["err"] += v - prev_v
                prev_t, prev_v = t, v
    for (svc, t1), a in sorted(agg.items()):
        t0 = a["t0"]
        dt = (t1 - t0) / 1e9 if t0 else 0
        if dt <= 0:
            continue
        out.append(_blank(modality="metric", source="otlp", service=svc, operation="app.requests", ts_start=t0,
                          ts_end=t1, measurement="request_rate", value=round(a["req"] / dt, 6), unit="1/s"))
        if a["req"] > 0:
            out.append(_blank(modality="metric", source="otlp", service=svc, operation="app.errors", ts_start=t0,
                              ts_end=t1, measurement="error_ratio", value=round(a["err"] / a["req"], 6), unit="1"))
        if a["dcount"] > 0:
            out.append(_blank(modality="metric", source="otlp", service=svc, operation="app.request.duration",
                              ts_start=t0, ts_end=t1, measurement="latency_mean",
                              value=round(a["dsum"] / a["dcount"], 6), unit="ms"))
    return out


def normalize(case: dict) -> list[dict]:
    """Raw case (evidencerca.rca_input.load_case) -> evidence records with content ids, time-ordered."""
    sig = case["signals"]
    recs = spans_to_records(sig.get("traces", [])) + logs_to_records(sig.get("logs", [])) + \
        metrics_to_records(sig.get("metrics", []))
    recs = [r for r in recs if r["service"] in ALL_SERVICES]
    recs.sort(key=lambda r: (r["ts_start"], r["modality"], r["service"] or "", r["measurement"]))
    return assign_ids(recs, case["case_id"])


# ---------------------------------------------------------------- view
@dataclass
class View:
    """What a method sees: one deterministic evidence view of one case.

    `unavailable` lists modalities that were not collected for this view; `time_tolerance_ns`
    is the timestamp uncertainty of the view (non-zero for jittered views). Nothing else about
    how the view was produced is exposed.
    """
    view_id: str
    case_id: str
    records: list[dict]
    unavailable: frozenset = frozenset()
    time_tolerance_ns: int = 0
    by_id: dict = field(init=False, repr=False)
    _idx: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        self.records = sorted(self.records, key=lambda r: r["ts_start"])
        self.by_id = {r["evidence_id"]: r for r in self.records}
        idx = defaultdict(list)
        for r in self.records:
            idx[("mod", r["modality"])].append(r)
            idx[("svc", r["service"])].append(r)
            idx[("meas", r["service"], r["measurement"])].append(r)
            if r["peer_service"]:
                idx[("peer", r["peer_service"])].append(r)
        self._idx = dict(idx)

    @property
    def t_min(self) -> int:
        return min((r["ts_start"] for r in self.records), default=0)

    @property
    def t_max(self) -> int:
        return max((r["ts_end"] for r in self.records), default=0)

    def select(self, modality=None, service=None, measurement=None, kind=None, peer=None, t0=None, t1=None):
        if service and measurement:
            base = self._idx.get(("meas", service, measurement), [])
        elif peer:
            base = self._idx.get(("peer", peer), [])
        elif service:
            base = self._idx.get(("svc", service), [])
        elif modality:
            base = self._idx.get(("mod", modality), [])
        else:
            base = self.records
        if t0 is not None or t1 is not None:   # every index list is time-ordered
            keys = [r["ts_start"] for r in base] if len(base) < 64 else None
            lo = 0 if t0 is None else bisect_left(keys if keys is not None else _Keys(base), t0)
            hi = len(base) if t1 is None else bisect_left(keys if keys is not None else _Keys(base), t1)
            base = base[lo:hi]
        out = []
        for r in base:
            if modality and r["modality"] != modality:
                continue
            if service and r["service"] != service:
                continue
            if measurement and r["measurement"] != measurement:
                continue
            if kind and r["kind"] != kind:
                continue
            if peer and r["peer_service"] != peer:
                continue
            if t0 is not None and r["ts_start"] < t0:
                continue
            if t1 is not None and r["ts_start"] >= t1:
                continue
            out.append(r)
        return out


class _Keys:
    """Lazy ts_start view of a time-ordered record list, for bisect."""
    __slots__ = ("rs",)

    def __init__(self, rs):
        self.rs = rs

    def __len__(self):
        return len(self.rs)

    def __getitem__(self, i):
        return self.rs[i]["ts_start"]


def view_id_for(case_id: str, spec: str, seed: int) -> str:
    """Opaque view id = hash(case_id, perturbation spec, perturbation seed) (protocol §1)."""
    return "vw-" + hashlib.sha256(f"{case_id}|{spec}|{seed}".encode()).hexdigest()[:16]
