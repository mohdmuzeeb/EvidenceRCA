"""Evidence verifier V1-V6 (protocol §6.2; full specification in docs/evidence_verifier_spec.md).

Independence: this module does not import the pipeline, the signal layer or the baselines. It
re-derives everything it needs (baseline window, robust statistics, observed call edges, onsets)
from the View with its own code, so pipeline bugs are not inherited. It never reads ground truth
or fault labels: its only inputs are a Diagnosis JSON and the View it was produced on.

Checks per claim, each "pass" | "fail" | "not_applicable":
  V0 schema    closed vocabulary, required keys, known unit
  V1 exists    every cited evidence id is in the current view
  V2 identity  case/view ids match; every cited record lies in the claim's time window (+ tolerance)
  V3 assoc.    every cited record belongs to the subject (and object) service with the modality and
               measurement the predicate allows
  V4 value     the claimed value and unit agree with the value recomputed from the view (5 % relative)
  V5 support   the predicate holds against a baseline the verifier computes itself
  V6 topology  (per propagation edge) the edge exists in the view's observed call graph
Observed claims are verified only if every applicable check passes. Inferred claims (causal
hypotheses) are checked for V0-V3 on their premises only and are never counted as verified facts.
Failed claims are rejected and reported, never rewritten.
"""

import statistics

VOCAB = ("latency_above_baseline", "error_rate_above_baseline", "no_telemetry_from", "cpu_above_baseline",
         "pool_wait_above_baseline", "log_error_pattern", "call_failed", "timeout_observed", "onset_before")
KINDS = ("observed", "inferred")
REQUIRED = {"claim_id", "kind", "subject_service", "predicate", "value", "unit", "window", "evidence_ids"}
UNITS = {  # predicate -> {unit: factor to the canonical unit}
    "latency_above_baseline": {"ms": 1.0, "s": 1000.0},
    "error_rate_above_baseline": {"1": 1.0, "%": 0.01},
    "no_telemetry_from": {"s": 1.0, "ms": 0.001},
    "cpu_above_baseline": {"%": 1.0, "1": 1.0},
    "pool_wait_above_baseline": {"{connection}": 1.0, "ms": 1.0},
    "log_error_pattern": {"{record}": 1.0},
    "call_failed": {"{call}": 1.0},
    "timeout_observed": {"{call}": 1.0},
    "onset_before": {"s": 1.0, "ms": 0.001},
}
REL_TOL = 0.05
SEC = 10**9
BASELINE_NS, STARTUP_NS, BUCKET = 90 * SEC, 30 * SEC, 5 * SEC
PERIODIC = ("container_cpu", "process_cpu")
MIN_BASELINE = 3


# ---------------------------------------------------------------- independent helpers
def _median_mad(xs):
    m = statistics.median(xs)
    return m, 1.4826 * statistics.median([abs(x - m) for x in xs])


def _baseline_window(view, w0):
    return max(w0 - BASELINE_NS, view.t_min + STARTUP_NS), w0


def _in(r, t0, t1):
    return t0 <= r["ts_start"] < t1


def _assoc(r, predicate, subject) -> bool:
    """Does record r belong to `subject` in the way `predicate` requires?"""
    m, mod = r["measurement"], r["modality"]
    server_of = mod == "trace" and r["kind"] == "SERVER" and r["service"] == subject
    client_to = mod == "trace" and r["kind"] == "CLIENT" and r["peer_service"] == subject
    if predicate == "latency_above_baseline":
        return server_of or client_to or (mod == "metric" and m == "latency_mean" and r["service"] == subject)
    if predicate == "error_rate_above_baseline":
        return server_of or (mod == "metric" and m == "error_ratio" and r["service"] == subject)
    if predicate in ("call_failed", "timeout_observed"):
        return client_to
    if predicate == "no_telemetry_from":
        return mod == "metric" and m in PERIODIC and r["service"] == subject
    if predicate == "cpu_above_baseline":
        return mod == "metric" and m in ("container_cpu", "process_cpu") and r["service"] == subject
    if predicate == "pool_wait_above_baseline":
        local_connect = (mod == "trace" and r["kind"] == "CLIENT" and r["service"] == subject
                         and r["operation"] == "connect" and r["peer_service"] is None)
        return local_connect or (mod == "metric" and m == "pool_checked_out" and r["service"] == subject)
    if predicate == "log_error_pattern":
        return mod == "log" and r["service"] == subject
    return r["service"] == subject or r.get("peer_service") == subject   # onset_before / inferred premises


_REL_CACHE: dict = {}


def _related(view, subject):
    """Records emitted by `subject` or calling it (own lookup, built once per view object)."""
    key = (id(view), view.view_id)
    if key not in _REL_CACHE:
        if len(_REL_CACHE) > 8:
            _REL_CACHE.clear()
        idx = {}
        for r in view.records:
            idx.setdefault(r["service"], []).append(r)
            if r.get("peer_service") and r["peer_service"] != r["service"]:
                idx.setdefault(r["peer_service"], []).append(r)
        _REL_CACHE[key] = idx
    return _REL_CACHE[key].get(subject, [])


def _population(view, predicate, subject, t0, t1, like=None):
    """All records of the view associated with subject for this predicate in [t0, t1), optionally restricted
    to the same (modality, measurement, kind) family as `like`."""
    out = []
    for r in _related(view, subject):
        if not _in(r, t0, t1) or not _assoc(r, predicate, subject):
            continue
        if like is not None and (r["modality"], r["measurement"], r["kind"], r["operation"] == "connect") != \
                (like["modality"], like["measurement"], like["kind"], like["operation"] == "connect"):
            continue
        out.append(r)
    return out


def call_edges(view) -> set:
    """Caller -> callee pairs observed in this view's spans (own extraction, independent of the pipeline)."""
    spans = [r for r in view.records if r["modality"] == "trace"]
    by_span = {r["span_id"]: r for r in spans}
    edges = set()
    for r in spans:
        if r["kind"] == "CLIENT" and r["peer_service"] and r["peer_service"] != r["service"]:
            edges.add((r["service"], r["peer_service"]))
        if r["kind"] == "SERVER" and r["parent_span_id"] in by_span:
            p = by_span[r["parent_span_id"]]
            if p["kind"] == "CLIENT" and p["service"] != r["service"]:
                edges.add((p["service"], r["service"]))
    return edges


_BASE_CACHE: dict = {}


def _exceeds(r, view, predicate, subject, w0):
    family = (r["modality"], r["measurement"], r["kind"], r["operation"] == "connect")
    key = (id(view), view.view_id, predicate, subject, family, w0)
    if key not in _BASE_CACHE:
        b0, b1 = _baseline_window(view, w0)
        base = [x["value"] for x in _population(view, predicate, subject, b0, b1, like=r)]
        _BASE_CACHE[key] = _median_mad(base) if len(base) >= MIN_BASELINE else None
        if len(_BASE_CACHE) > 20000:
            _BASE_CACHE.clear()
    stats = _BASE_CACHE[key]
    if stats is None:
        return None
    m, mad = stats
    return r["value"] > m + 3 * mad and r["value"] > m


def _periodic_gap(view, subject, w0, w1):
    """(all streams with a baseline cadence are silent, max gap s, detail)."""
    b0, b1 = _baseline_window(view, w0)
    detail, silent, gaps = {}, [], []
    for meas in PERIODIC:
        ts = sorted(r["ts_start"] for r in view.records if r["service"] == subject and r["measurement"] == meas)
        base = [t for t in ts if b0 <= t < b1]
        if len(base) < 6:
            detail[meas] = "no expected-stream baseline"
            continue
        d = statistics.median([b - a for a, b in zip(base, base[1:])])
        prior = [t for t in ts if t < w0]
        pts = ([prior[-1]] if prior else [w0]) + [t for t in ts if w0 <= t < w1] + [w1]
        g = max(b - a for a, b in zip(pts, pts[1:]))
        gaps.append(g / SEC)
        silent.append(g >= 3 * d)
        detail[meas] = {"cadence_s": d / SEC, "max_gap_s": g / SEC}
    return (bool(silent) and all(silent)), (max(gaps) if gaps else None), detail


def _onset(view, subject, w0, w1):
    """Earliest record in the window associated with `subject` that exceeds its own baseline (any predicate)."""
    best = None
    for pred in ("latency_above_baseline", "error_rate_above_baseline", "cpu_above_baseline",
                 "pool_wait_above_baseline", "call_failed"):
        for r in _population(view, pred, subject, w0, w1):
            if pred in ("error_rate_above_baseline", "call_failed"):
                ok = r["status"] == "error" or (r["modality"] == "metric" and r["value"] > 0.05)
            else:
                ok = _exceeds(r, view, pred, subject, w0)
            if ok and (best is None or r["ts_start"] < best):
                best = r["ts_start"]
    silent, _, detail = _periodic_gap(view, subject, w0, w1)
    if silent:
        starts = []
        for meas in PERIODIC:
            prior = [r["ts_start"] for r in view.records if r["service"] == subject and r["measurement"] == meas
                     and r["ts_start"] < w1]
            if prior:
                starts.append(max(prior))
        if starts and (best is None or min(starts) < best):
            best = min(starts)
    return best


# ---------------------------------------------------------------- per-claim verification
def _recompute(claim, view, recs):
    p, subj = claim["predicate"], claim["subject_service"]
    w0, w1 = claim["window"]
    if p in ("latency_above_baseline", "cpu_above_baseline", "pool_wait_above_baseline") or (
            p == "error_rate_above_baseline" and claim.get("measurement") == "error_ratio"):
        return statistics.median_high([r["value"] for r in recs]) if recs else None
    if p == "error_rate_above_baseline":
        pop = [r for r in view.records if _in(r, w0, w1) and r["modality"] == "trace" and r["kind"] == "SERVER"
               and r["service"] == subj]
        return sum(r["status"] == "error" for r in pop) / len(pop) if pop else None
    if p == "call_failed":
        return float(sum(r["status"] == "error" for r in _population(view, p, subj, w0, w1)))
    if p == "timeout_observed":
        return float(sum("timeout" in (r["error_type"] or "").lower() for r in _population(view, p, subj, w0, w1)))
    if p == "log_error_pattern":
        return float(sum((r["severity"] or 0) >= 17 for r in _population(view, p, subj, w0, w1)))
    if p == "no_telemetry_from":
        return _periodic_gap(view, subj, w0, w1)[1]
    if p == "onset_before":   # V4 checks the number against the cited records; V5 checks onsets independently
        subj_t = [r["ts_start"] for r in recs if _assoc(r, p, subj)]
        obj_t = [r["ts_start"] for r in recs if _assoc(r, p, claim.get("object_service"))]
        return None if not subj_t or not obj_t else (min(obj_t) - min(subj_t)) / SEC
    return None


def _support(claim, view, recs):
    p, subj = claim["predicate"], claim["subject_service"]
    w0, w1 = claim["window"]
    b0, b1 = _baseline_window(view, w0)
    if p in ("latency_above_baseline", "cpu_above_baseline", "pool_wait_above_baseline") or (
            p == "error_rate_above_baseline" and claim.get("measurement") == "error_ratio"):
        res = [_exceeds(r, view, p, subj, w0) for r in recs]
        if any(x is None for x in res):
            return False, "insufficient baseline for a cited record"
        return all(res), f"{sum(res)}/{len(res)} cited records exceed baseline median + 3 MAD"
    if p == "error_rate_above_baseline":
        def rate(t0, t1):
            pop = [r for r in view.records if _in(r, t0, t1) and r["modality"] == "trace" and r["kind"] == "SERVER"
                   and r["service"] == subj]
            return (sum(r["status"] == "error" for r in pop) / len(pop)) if pop else None
        rw, rb = rate(w0, w1), rate(b0, b1)
        if rw is None or rb is None:
            return False, "no spans in window or baseline"
        return (rw >= rb + 0.05 and all(r["status"] == "error" for r in recs)), f"window {rw:.3f} vs baseline {rb:.3f}"
    if p in ("call_failed", "timeout_observed"):
        def frac(t0, t1):
            pop = _population(view, p, subj, t0, t1)
            if not pop:
                return None
            if p == "call_failed":
                return sum(r["status"] == "error" for r in pop) / len(pop)
            return sum("timeout" in (r["error_type"] or "").lower() for r in pop) / len(pop)
        fw, fb = frac(w0, w1), frac(b0, b1)
        cited_ok = all((r["status"] == "error") if p == "call_failed" else
                       ("timeout" in (r["error_type"] or "").lower()) for r in recs)
        if fw is None or fb is None:
            return False, "no calls in window or baseline"
        return (cited_ok and fw >= fb + 0.05), f"window {fw:.3f} vs baseline {fb:.3f}; cited all match={cited_ok}"
    if p == "log_error_pattern":
        cnt = lambda t0, t1: sum((r["severity"] or 0) >= 17 for r in _population(view, p, subj, t0, t1))
        cw, cb = cnt(w0, w1), cnt(b0, b1)
        cb_scaled = cb * (w1 - w0) / max(b1 - b0, 1)
        return (all((r["severity"] or 0) >= 17 for r in recs) and cw > cb_scaled + 2), f"{cw} vs {cb_scaled:.1f}"
    if p == "no_telemetry_from":
        silent, gap, detail = _periodic_gap(view, subj, w0, w1)
        return silent, f"expected streams: {detail}"
    if p == "onset_before":
        a, b = _onset(view, subj, w0, w1), _onset(view, claim.get("object_service"), w0, w1)
        if a is None or b is None:
            return False, "onset not observable for one of the services"
        return a <= b + BUCKET + view.time_tolerance_ns, f"onset lead {(b - a) / SEC:.2f} s"
    return False, "unsupported predicate"


def verify_claim(claim: dict, diag: dict, view) -> dict:
    res = {"claim_id": claim.get("claim_id"), "kind": claim.get("kind"), "predicate": claim.get("predicate"),
           "subject_service": claim.get("subject_service"), "checks": {}, "notes": {}}
    c = res["checks"]
    schema_ok = REQUIRED <= set(claim) and claim.get("predicate") in VOCAB and claim.get("kind") in KINDS and \
        isinstance(claim.get("evidence_ids"), list) and len(claim.get("window") or []) == 2
    if schema_ok and claim["kind"] == "observed":
        schema_ok = claim.get("unit") in UNITS[claim["predicate"]] and claim.get("value") is not None
        if claim["predicate"] == "onset_before":
            schema_ok = schema_ok and bool(claim.get("object_service"))
    c["V0"] = "pass" if schema_ok else "fail"
    if not schema_ok:
        res["verified"] = False
        return res
    ids = claim["evidence_ids"]
    missing = [e for e in ids if e not in view.by_id]
    c["V1"] = "pass" if ids and not missing else "fail"
    res["notes"]["V1_missing"] = missing
    recs = [view.by_id[e] for e in ids if e in view.by_id]
    w0, w1 = claim["window"]
    tol = view.time_tolerance_ns
    lo = w0 - (BASELINE_NS if claim["predicate"] in ("no_telemetry_from",) else 0) - tol
    ident = diag.get("case_id") == view.case_id and diag.get("view_id") == view.view_id and \
        all(r["case_id"] == view.case_id for r in recs)
    in_time = all(lo <= r["ts_start"] <= w1 + tol for r in recs)
    c["V2"] = "pass" if recs and ident and in_time else "fail"
    subj, obj = claim["subject_service"], claim.get("object_service")
    if claim["predicate"] == "onset_before":
        assoc = any(_assoc(r, "onset_before", subj) for r in recs) and any(_assoc(r, "onset_before", obj) for r in recs)
    elif claim["kind"] == "inferred":
        assoc = all(_assoc(r, "onset_before", subj) for r in recs)
    else:
        assoc = all(_assoc(r, claim["predicate"], subj) for r in recs)
    c["V3"] = "pass" if recs and assoc else "fail"
    if claim["kind"] == "inferred":
        c["V4"] = c["V5"] = "not_applicable"
        res["premises_valid"] = all(c[k] == "pass" for k in ("V1", "V2", "V3"))
        res["verified"] = False   # a causal hypothesis is never a verified fact
        return res
    factor = UNITS[claim["predicate"]][claim["unit"]]
    value = float(claim["value"]) * factor
    got = _recompute(claim, view, recs)
    res["notes"]["V4_recomputed"] = got
    if got is None:
        c["V4"] = "fail"
    elif claim["predicate"] == "onset_before":
        c["V4"] = "pass" if abs(value - got) <= 1e-3 + tol / SEC else "fail"
    else:
        c["V4"] = "pass" if abs(value - got) <= REL_TOL * max(abs(got), 1e-9) else "fail"
    ok, why = _support(claim, view, recs)
    c["V5"] = "pass" if ok else "fail"
    res["notes"]["V5"] = why
    res["verified"] = all(v == "pass" for v in c.values())
    return res


def verify(diag: dict, view) -> dict:
    """Verify every claim and propagation edge of a diagnosis against the view it was produced on."""
    claims = [verify_claim(cl, diag, view) for cl in diag.get("factual_claims") or []]
    edges = call_edges(view)
    path = []
    for i, e in enumerate(diag.get("propagation_path") or []):
        src, dst = e.get("from"), e.get("to")
        exists = (dst, src) in edges   # propagation src -> dst requires the call dst -> src
        cited = [view.by_id.get(x) for x in e.get("evidence_ids", [])]
        cited_ok = all(r is not None and r["modality"] == "trace" and r["service"] == dst and r["peer_service"] == src
                       for r in cited)
        path.append({"edge": f"{src}->{dst}", "V6": "pass" if exists else "fail",
                     "evidence": "pass" if cited_ok else "fail"})
    observed = [c for c in claims if c["kind"] == "observed"]
    verified_obs = [c for c in observed if c["verified"]]
    suspect = diag.get("suspected_service")
    flags = []
    if diag.get("decision") == "DIAGNOSE" and not any(c["subject_service"] == suspect for c in verified_obs):
        flags.append("DIAGNOSE without a verified observed claim about the suspected service")
    return {"case_id": diag.get("case_id"), "view_id": diag.get("view_id"), "method": diag.get("method"),
            "claims": claims, "propagation": path,
            "summary": {"observed": len(observed), "observed_verified": len(verified_obs),
                        "observed_rejected": len(observed) - len(verified_obs),
                        "inferred": len(claims) - len(observed),
                        "inferred_premises_valid": sum(c.get("premises_valid", False) for c in claims
                                                       if c["kind"] == "inferred"),
                        "edges": len(path), "edges_verified": sum(p["V6"] == "pass" and p["evidence"] == "pass"
                                                                  for p in path),
                        "verified_claim_coverage": (len(verified_obs) / len(observed)) if observed else None},
            "rejected_claim_ids": [c["claim_id"] for c in observed if not c["verified"]],
            "flags": flags}
