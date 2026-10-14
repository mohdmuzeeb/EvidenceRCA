"""LLM output schema, an independent validator, and conversion to the frozen Diagnosis JSON (agent side).

The JSON schema is passed to Ollama as `format` (grammar-constrained decoding), but compliance is never
assumed: every response is parsed and validated here with our own code. Outcomes are kept distinct:
  malformed_json        the text is not one JSON object (e.g. truncated at the token limit)
  schema_noncompliant   valid JSON that violates the schema (missing keys, wrong types/enums, too many items)
  valid                 schema-compliant; semantic problems (unknown aliases, DIAGNOSE with "none", ...) are
                        reported separately as `semantic_issues` and left in the claims for the verifier.

Conversion never edits what the model claimed. It only maps aliases back to evidence ids (unknown aliases are
kept verbatim so the verifier's V1 rejects them), fills ids/window from the case, sets value=None for inferred
claims, and derives `measurement` from the cited records (the verifier uses it only to pick the error-rate
recomputation).
"""

import json

from evidencerca.evidence import ALL_SERVICES
from evidencerca.methods.diagnosis import FAULT_TYPES, PREDICATES, empty_diagnosis

UNITS = ["ms", "s", "1", "%", "{call}", "{record}", "{connection}"]
MAX_CLAIMS, MAX_EDGES, MAX_CITES, MAX_CANDS, MAX_REASONS = 6, 4, 5, 5, 3
SVC_OR_NONE = list(ALL_SERVICES) + ["none"]

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["DIAGNOSE", "ABSTAIN"]},
        "suspected_service": {"type": "string", "enum": SVC_OR_NONE},
        "suspected_fault_type": {"type": "string", "enum": list(FAULT_TYPES) + ["unknown"]},
        "ranked_candidates": {"type": "array", "items": {"type": "string", "enum": list(ALL_SERVICES)},
                              "maxItems": MAX_CANDS},
        "claims": {"type": "array", "maxItems": MAX_CLAIMS, "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["observed", "inferred"]},
                           "predicate": {"type": "string", "enum": list(PREDICATES)},
                           "subject_service": {"type": "string", "enum": list(ALL_SERVICES)},
                           "object_service": {"type": "string", "enum": SVC_OR_NONE},
                           "value": {"type": "number"},
                           "unit": {"type": "string", "enum": UNITS},
                           "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CITES}},
            "required": ["kind", "predicate", "subject_service", "object_service", "value", "unit", "evidence"]}},
        "propagation_path": {"type": "array", "maxItems": MAX_EDGES, "items": {
            "type": "object",
            "properties": {"from": {"type": "string", "enum": list(ALL_SERVICES)},
                           "to": {"type": "string", "enum": list(ALL_SERVICES)},
                           "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CITES}},
            "required": ["from", "to", "evidence"]}},
        "uncertainty_reasons": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_REASONS},
        "explanation": {"type": "string"},
    },
    "required": ["decision", "suspected_service", "suspected_fault_type", "ranked_candidates", "claims",
                 "propagation_path", "uncertainty_reasons", "explanation"],
}


def _check(value, schema, path, errors):
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


def semantic_issues(obj: dict, alias_map: dict) -> list[str]:
    out = []
    if obj.get("decision") == "DIAGNOSE" and obj.get("suspected_service") == "none":
        out.append("DIAGNOSE with suspected_service none")
    if obj.get("decision") == "ABSTAIN" and obj.get("suspected_service") not in (None, "none"):
        out.append("ABSTAIN with a named suspected_service")
    for i, c in enumerate(obj.get("claims") or []):
        unknown = [e for e in c.get("evidence", []) if e not in alias_map]
        if unknown:
            out.append(f"claims[{i}] cites unknown aliases {unknown}")
        if not c.get("evidence"):
            out.append(f"claims[{i}] cites nothing")
        if c.get("predicate") == "onset_before" and c.get("object_service") in (None, "none"):
            out.append(f"claims[{i}] onset_before without object_service")
    for i, e in enumerate(obj.get("propagation_path") or []):
        unknown = [x for x in e.get("evidence", []) if x not in alias_map]
        if unknown:
            out.append(f"propagation_path[{i}] cites unknown aliases {unknown}")
    return out


def _measurement(predicate, recs):
    meas = sorted({r["measurement"] for r in recs})
    if predicate == "error_rate_above_baseline":
        return "error_ratio" if recs and meas == ["error_ratio"] else "span_error_ratio"
    return meas[0] if len(meas) == 1 else None


def to_diagnosis(obj: dict, view, windows, alias_map: dict, method: str) -> dict:
    """Frozen Diagnosis JSON (evidencerca.methods.diagnosis) carrying the model's own decision and claims."""
    d = empty_diagnosis(view, method, windows)
    a0, a1 = windows.analysis
    claims = []
    for i, c in enumerate(obj.get("claims") or []):
        ids = [alias_map.get(e, e) for e in c.get("evidence", [])]
        recs = [view.by_id[x] for x in ids if x in view.by_id]
        obs = c.get("kind") == "observed"
        claims.append({"claim_id": f"c{i + 1}", "kind": c.get("kind"), "subject_service": c.get("subject_service"),
                       "object_service": None if c.get("object_service") in (None, "none") else c["object_service"],
                       "predicate": c.get("predicate"), "measurement": _measurement(c.get("predicate"), recs),
                       "value": c.get("value") if obs else None, "unit": c.get("unit") if obs else None,
                       "window": [a0, a1], "evidence_ids": ids})
    d["factual_claims"] = claims
    d["propagation_path"] = [{"from": e.get("from"), "to": e.get("to"),
                              "evidence_ids": [alias_map.get(x, x) for x in e.get("evidence", [])]}
                             for e in obj.get("propagation_path") or []]
    d["evidence_ids"] = sorted({e for c in claims for e in c["evidence_ids"]})
    d["uncertainty_reasons"] = list(obj.get("uncertainty_reasons") or [])
    d["explanation"] = obj.get("explanation")
    return d
