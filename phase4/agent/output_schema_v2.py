"""p4a-v2 typed output schema, its validator, and the typed claim checks (agent side; p4a-v1 is untouched).

Changes from p4a-v1 (phase4.agent.output_schema):
  * typed claims: one schema branch per predicate (anyOf) fixing the measure class (count | duration | ratio |
    resource) and the allowed units, so a count claim cannot carry "1" or "ms";
  * explicit ownership: every citation is {id, emitter, record} -- the service that emitted the record and the
    record type (server | call | metric | log | connect);
  * propagation edges are written root side -> affected dependent ("from" is the suspected origin);
  * an explicit `evidence_strength` field precedes the decision (keys are generated in schema order);
  * smaller output budget per answer (<= 5 claims, <= 3 citations, <= 3 edges) under the same num_predict.

Validation has two layers, both run on agent-visible data only (the record table and call graph the model saw):
  parse()          JSON -> malformed_json | schema_noncompliant | valid (as in v1; now with anyOf / minItems)
  typed_checks()   per claim and per edge: declared ownership matches the cited record, the record type is one
                   the predicate admits, the subject/object association holds, and every edge exists in the
                   observed call graph in the stated direction. A claim or edge failing a typed check is
                   rejected at validation; it is still kept verbatim in the pre-filter set and still sent to the
                   frozen verifier, which judges every claim independently. The typed checks never look at
                   values or baselines (that is V4/V5's job) and never edit a claim.
"""

import json

from evidencerca.evidence import ALL_SERVICES
from evidencerca.methods.diagnosis import FAULT_TYPES, empty_diagnosis

MAX_CLAIMS, MAX_EDGES, MAX_CITES, MAX_CANDS, MAX_REASONS = 5, 3, 3, 5, 3
SVC_OR_NONE = list(ALL_SERVICES) + ["none"]
ALIASES = [f"E{i:02d}" for i in range(1, 37)]            # case-invariant: the pack holds at most 36 records
RECORD_TYPES = ["server", "call", "metric", "log", "connect"]
STRENGTH = ["clear_anomaly", "weak_or_no_deviation", "conflicting"]

# predicate -> (measure class, allowed units, admissible record types) -- a strict subset of the frozen
# verifier's units (evidencerca.verifier.UNITS), one canonical unit per record family.
TYPES = {
    "latency_above_baseline": ("duration", ["ms"], ["server", "call", "metric"]),
    "error_rate_above_baseline": ("ratio", ["1"], ["server", "metric"]),
    "call_failed": ("count", ["{call}"], ["call"]),
    "timeout_observed": ("count", ["{call}"], ["call"]),
    "log_error_pattern": ("count", ["{record}"], ["log"]),
    "no_telemetry_from": ("duration", ["s"], ["metric"]),
    "cpu_above_baseline": ("resource", ["%", "1"], ["metric"]),
    "pool_wait_above_baseline": (["duration", "resource"], ["ms", "{connection}"], ["connect", "metric"]),
    "onset_before": ("duration", ["s"], ["server", "call", "metric", "log", "connect"]),
}
METRIC_FOR = {"latency_above_baseline": {"latency_mean"}, "error_rate_above_baseline": {"error_ratio"},
              "no_telemetry_from": {"container_cpu", "process_cpu"}, "cpu_above_baseline": {"container_cpu", "process_cpu"},
              "pool_wait_above_baseline": {"pool_checked_out"}}
UNIT_FOR_RECORD = {("cpu_above_baseline", "container_cpu"): "%", ("cpu_above_baseline", "process_cpu"): "1",
                   ("pool_wait_above_baseline", "connect"): "ms", ("pool_wait_above_baseline", "pool_checked_out"): "{connection}"}
MEASURE_FOR_UNIT = {("pool_wait_above_baseline", "ms"): "duration", ("pool_wait_above_baseline", "{connection}"): "resource"}

_CITE = {"type": "object",
         "properties": {"id": {"type": "string", "enum": ALIASES},
                        "emitter": {"type": "string", "enum": list(ALL_SERVICES)},
                        "record": {"type": "string", "enum": RECORD_TYPES}},
         "required": ["id", "emitter", "record"]}


def _claim_branch(pred):
    measure, units, _ = TYPES[pred]
    return {"type": "object",
            "properties": {"kind": {"type": "string", "enum": ["observed", "inferred"]},
                           "predicate": {"type": "string", "enum": [pred]},
                           "subject_service": {"type": "string", "enum": list(ALL_SERVICES)},
                           "object_service": {"type": "string", "enum": list(ALL_SERVICES) if pred == "onset_before"
                                              else ["none"]},
                           "measure": {"type": "string", "enum": measure if isinstance(measure, list) else [measure]},
                           "value": {"type": "number"},
                           "unit": {"type": "string", "enum": units},
                           "evidence": {"type": "array", "items": _CITE, "minItems": 1, "maxItems": MAX_CITES}},
            "required": ["kind", "predicate", "subject_service", "object_service", "measure", "value", "unit",
                         "evidence"]}


SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {"type": "array", "maxItems": MAX_CLAIMS, "items": {"anyOf": [_claim_branch(p) for p in TYPES]}},
        "propagation_path": {"type": "array", "maxItems": MAX_EDGES, "items": {
            "type": "object",
            "properties": {"from": {"type": "string", "enum": list(ALL_SERVICES)},
                           "to": {"type": "string", "enum": list(ALL_SERVICES)},
                           "evidence": {"type": "array", "items": _CITE, "minItems": 1, "maxItems": MAX_CITES}},
            "required": ["from", "to", "evidence"]}},
        "evidence_strength": {"type": "string", "enum": STRENGTH},
        "uncertainty_reasons": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_REASONS},
        "decision": {"type": "string", "enum": ["DIAGNOSE", "ABSTAIN"]},
        "suspected_service": {"type": "string", "enum": SVC_OR_NONE},
        "suspected_fault_type": {"type": "string", "enum": list(FAULT_TYPES) + ["unknown"]},
        "ranked_candidates": {"type": "array", "items": {"type": "string", "enum": list(ALL_SERVICES)},
                              "maxItems": MAX_CANDS},
        "explanation": {"type": "string"},
    },
    "required": ["claims", "propagation_path", "evidence_strength", "uncertainty_reasons", "decision",
                 "suspected_service", "suspected_fault_type", "ranked_candidates", "explanation"],
}


def _check(value, schema, path, errors):
    if "anyOf" in schema:
        branch_errs = []
        for sub in schema["anyOf"]:
            e = []
            _check(value, sub, path, e)
            if not e:
                return
            branch_errs.append(e)
        # report the branch the value claims to be (same predicate), else a generic message
        pred = value.get("predicate") if isinstance(value, dict) else None
        named = [e for sub, e in zip(schema["anyOf"], branch_errs)
                 if pred in sub.get("properties", {}).get("predicate", {}).get("enum", [])]
        errors.extend(named[0] if named else [f"{path}: matches no typed claim branch"])
        return
    t = schema.get("type")
    ok_type = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str),
               "number": isinstance(value, (int, float)) and not isinstance(value, bool)}.get(t, True)
    if not ok_type:
        errors.append(f"{path}: expected {t}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if t == "number" and value != value:
        errors.append(f"{path}: NaN")
    if t == "array":
        if len(value) > schema.get("maxItems", 10**9):
            errors.append(f"{path}: {len(value)} items > maxItems {schema['maxItems']}")
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: {len(value)} items < minItems {schema['minItems']}")
        for i, v in enumerate(value):
            _check(v, schema["items"], f"{path}[{i}]", errors)
    if t == "object":
        for k in schema.get("required", []):
            if k not in value:
                errors.append(f"{path}.{k}: missing")
        extra = set(value) - set(schema.get("properties", {}))
        if extra:
            errors.append(f"{path}: unexpected keys {sorted(extra)}")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                _check(value[k], sub, f"{path}.{k}", errors)


def parse(text: str | None) -> dict:
    """{status, obj, errors}; status in malformed_json | schema_noncompliant | valid."""
    if not text or not text.strip():
        return {"status": "malformed_json", "obj": None, "errors": ["empty response"]}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"status": "malformed_json", "obj": None, "errors": [f"json: {exc.msg} at {exc.pos}"]}
    if not isinstance(obj, dict):
        return {"status": "malformed_json", "obj": None, "errors": ["top level is not an object"]}
    errors = []
    _check(obj, SCHEMA, "$", errors)
    return {"status": "schema_noncompliant" if errors else "valid", "obj": obj, "errors": errors}


# ---------------------------------------------------------------- typed checks (agent-visible data only)
def record_type(r: dict) -> str:
    if r["modality"] == "trace":
        if r["kind"] == "CLIENT" and r["peer_service"] and r["peer_service"] != r["service"]:
            return "call"
        if r["kind"] == "CLIENT" and r["operation"] == "connect" and r["peer_service"] is None:
            return "connect"
        return "server" if r["kind"] == "SERVER" else "other_span"
    return r["modality"]          # metric | log


def _owned(r, pred, subj) -> str | None:
    """None if record r is admissible evidence about `subj` for `pred`, else the reason it is not."""
    rt = record_type(r)
    if rt not in TYPES[pred][2]:
        return f"{rt} records are not admissible for {pred}"
    if rt == "call":
        if r["peer_service"] != subj:
            return f"call {r['service']}->{r['peer_service']} is not a call to the subject {subj}"
    elif r["service"] != subj:
        return f"{rt} record emitted by {r['service']}, not by the subject {subj}"
    if rt == "metric" and r["measurement"] not in METRIC_FOR.get(pred, set()):
        return f"metric {r['measurement']} is not admissible for {pred}"
    if rt == "log" and (r["severity"] or 0) < 17:
        return "log record below error severity"
    if pred == "call_failed" and r["status"] != "error":
        return "cited call did not fail"
    if pred == "timeout_observed" and "timeout" not in (r["error_type"] or "").lower():
        return "cited call did not time out"
    return None


def typed_checks(obj: dict, alias_map: dict, records_by_id: dict, graph_edges: set) -> dict:
    """Per-claim and per-edge typed validation. graph_edges: observed (caller, callee) pairs from the pack."""
    claims = []
    for i, c in enumerate(obj.get("claims") or []):
        why, pred, subj = [], c.get("predicate"), c.get("subject_service")
        unit, cites = c.get("unit"), c.get("evidence") or []
        measure, units, _ = TYPES.get(pred, (None, [], []))
        if pred not in TYPES:
            why.append("unknown predicate")
        elif unit not in units:
            why.append(f"unit {unit!r} not allowed for {pred}")
        elif MEASURE_FOR_UNIT.get((pred, unit), measure) != c.get("measure"):
            why.append(f"measure {c.get('measure')!r} does not match {pred} in {unit}")
        recs = []
        for k, e in enumerate(cites):
            r = records_by_id.get(alias_map.get(e.get("id")))
            if r is None:
                why.append(f"evidence[{k}] {e.get('id')} is not in the record table")
                continue
            recs.append(r)
            if e.get("emitter") != r["service"]:
                why.append(f"evidence[{k}] {e['id']} declared emitter {e.get('emitter')}, emitted by {r['service']}")
            if e.get("record") != record_type(r):
                why.append(f"evidence[{k}] {e['id']} declared {e.get('record')}, is {record_type(r)}")
        if pred in TYPES and c.get("kind") == "observed" and pred != "onset_before":
            for k, r in enumerate(recs):
                bad = _owned(r, pred, subj)
                if bad:
                    why.append(f"evidence[{k}]: {bad}")
                exp = UNIT_FOR_RECORD.get((pred, r["measurement"] if record_type(r) == "metric" else record_type(r)))
                if exp and unit != exp:
                    why.append(f"evidence[{k}]: unit should be {exp} for this record")
            if c.get("object_service") not in (None, "none"):
                why.append("object_service must be none")
        if pred == "onset_before":
            obj_s = c.get("object_service")
            if obj_s in (None, "none") or obj_s == subj:
                why.append("onset_before needs an object service different from the subject")
            elif len(recs) != 2:
                why.append("onset_before cites exactly two records (subject first, object second)")
            else:
                a, b = recs
                if not (a["service"] == subj or a.get("peer_service") == subj):
                    why.append("first onset record is not associated with the subject")
                if not (b["service"] == obj_s and b.get("peer_service") != subj):
                    why.append("second onset record is not emitted by the object")
        if c.get("kind") == "inferred":
            if c.get("value") not in (0, 0.0):
                why.append("inferred claim with a non-zero value")
            for k, r in enumerate(recs):
                if not (r["service"] == subj or r.get("peer_service") == subj):
                    why.append(f"evidence[{k}]: premise not associated with the subject {subj}")
        claims.append({"index": i, "passed": not why, "reasons": why})
    edges = []
    for i, e in enumerate(obj.get("propagation_path") or []):
        why, src, dst = [], e.get("from"), e.get("to")
        if src == dst:
            why.append("self edge")
        elif (dst, src) not in graph_edges:
            why.append(f"no observed call {dst}->{src}; impact from {src} reaches {dst} only if {dst} calls {src}"
                       + (" (edge reversed)" if (src, dst) in graph_edges else ""))
        for k, c in enumerate(e.get("evidence") or []):
            r = records_by_id.get(alias_map.get(c.get("id")))
            if r is None:
                why.append(f"evidence[{k}] {c.get('id')} is not in the record table")
            elif not (record_type(r) == "call" and r["service"] == dst and r["peer_service"] == src):
                why.append(f"evidence[{k}] is not a call {dst}->{src}")
            elif c.get("emitter") != r["service"] or c.get("record") != "call":
                why.append(f"evidence[{k}] declared ownership does not match the record")
        edges.append({"index": i, "edge": f"{src}->{dst}", "passed": not why, "reasons": why})
    return {"claims": claims, "edges": edges}


def semantic_issues(obj: dict) -> list[str]:
    out = []
    dec, sus, strength = obj.get("decision"), obj.get("suspected_service"), obj.get("evidence_strength")
    if dec == "DIAGNOSE" and sus == "none":
        out.append("DIAGNOSE with suspected_service none")
    if dec == "ABSTAIN" and sus not in (None, "none"):
        out.append("ABSTAIN with a named suspected_service")
    if dec == "DIAGNOSE" and strength != "clear_anomaly":
        out.append(f"DIAGNOSE with evidence_strength {strength}")
    if dec == "ABSTAIN" and strength == "clear_anomaly":
        out.append("ABSTAIN with evidence_strength clear_anomaly")
    return out


def _measurement(predicate, recs):
    meas = sorted({r["measurement"] for r in recs})
    if predicate == "error_rate_above_baseline":
        return "error_ratio" if recs and meas == ["error_ratio"] else "span_error_ratio"
    return meas[0] if len(meas) == 1 else None


def to_diagnosis(obj: dict, view, windows, alias_map: dict, method: str) -> dict:
    """Frozen Diagnosis JSON carrying the model's own claims verbatim (aliases mapped to evidence ids; unknown
    aliases kept as written so V1 rejects them). The declared emitter/record type are not part of the frozen
    schema; they stay in the raw output and in the typed checks."""
    d = empty_diagnosis(view, method, windows)
    a0, a1 = windows.analysis
    claims = []
    for i, c in enumerate(obj.get("claims") or []):
        ids = [alias_map.get(e.get("id"), e.get("id")) for e in c.get("evidence", [])]
        recs = [view.by_id[x] for x in ids if x in view.by_id]
        obs = c.get("kind") == "observed"
        claims.append({"claim_id": f"c{i + 1}", "kind": c.get("kind"), "subject_service": c.get("subject_service"),
                       "object_service": None if c.get("object_service") in (None, "none") else c["object_service"],
                       "predicate": c.get("predicate"), "measurement": _measurement(c.get("predicate"), recs),
                       "value": c.get("value") if obs else None, "unit": c.get("unit") if obs else None,
                       "window": [a0, a1], "evidence_ids": ids})
    d["factual_claims"] = claims
    d["propagation_path"] = [{"from": e.get("from"), "to": e.get("to"),
                              "evidence_ids": [alias_map.get(x.get("id"), x.get("id")) for x in e.get("evidence", [])]}
                             for e in obj.get("propagation_path") or []]
    d["evidence_ids"] = sorted({e for c in claims for e in c["evidence_ids"]})
    d["uncertainty_reasons"] = list(obj.get("uncertainty_reasons") or [])
    d["explanation"] = obj.get("explanation")
    return d
