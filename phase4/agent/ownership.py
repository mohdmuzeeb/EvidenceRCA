"""Deterministic evidence ownership for E2 (agent side; design: docs/evidence_ownership_design.md).

Provenance of a cited record -- its emitter, span kind, callee, record family, measurement and unit -- is read
from the immutable telemetry metadata of the View. It is never taken from the model, and the model cannot
redefine it. From provenance and the claim's predicate, the admissible subject of the claim follows:

  family                 admissible for                                            subject
  server span            latency, error_rate                                       emitter
  call (CLIENT, callee)  latency, call_failed, timeout_observed                    callee (the caller's view of
                                                                                   calls to it; never the caller)
  connect span           pool_wait                                                 emitter
  metric latency_mean    latency                                                   emitter
  metric error_ratio     error_rate                                                emitter
  metric container_cpu / process_cpu   cpu, no_telemetry                           emitter
  metric pool_checked_out              pool_wait                                   emitter
  log                    log_error_pattern                                         emitter

Each original observed claim gets one disposition:
  consistent      every cited record exists, is admissible, and implies the claimed subject (unit admissible);
                  a stray object_service on a non-relational claim is only flagged (stray_object_service)
  transformed     every cited record exists and is admissible and they imply ONE subject, but the claim's
                  subject and/or an inadmissible unit differs (a stray object_service is then also cleared) -> a separate
                  canonical candidate is created with a full audit trail; the original is kept verbatim
  rejected        a cited record is missing, inadmissible for the predicate, or the records imply different
                  subjects -> no canonical candidate
  not_applicable  onset_before (relational) and inferred claims: kept as written, never transformed
Only subject, unit and object_service can change, and only to values fixed by provenance. Values, predicates,
windows and citations are never changed, so a transformed claim can still fail V4/V5.
"""

import hashlib
import json

from evidencerca.verifier import UNITS as VERIFIER_UNITS

TRANSFORM_VERSION = "own-v1"

ADMISSIBLE = {   # predicate -> {family: whose subject}
    "latency_above_baseline": {"server": "emitter", "call": "callee", "metric:latency_mean": "emitter"},
    "error_rate_above_baseline": {"server": "emitter", "metric:error_ratio": "emitter"},
    "call_failed": {"call": "callee"},
    "timeout_observed": {"call": "callee"},
    "no_telemetry_from": {"metric:container_cpu": "emitter", "metric:process_cpu": "emitter"},
    "cpu_above_baseline": {"metric:container_cpu": "emitter", "metric:process_cpu": "emitter"},
    "pool_wait_above_baseline": {"connect": "emitter", "metric:pool_checked_out": "emitter"},
    "log_error_pattern": {"log": "emitter"},
}
CANON_UNIT = {   # (predicate, family) -> unit fixed by the record family (used only when the claim's unit is
    ("latency_above_baseline", "server"): "ms", ("latency_above_baseline", "call"): "ms",      # not admissible)
    ("latency_above_baseline", "metric:latency_mean"): "ms",
    ("error_rate_above_baseline", "server"): "1", ("error_rate_above_baseline", "metric:error_ratio"): "1",
    ("call_failed", "call"): "{call}", ("timeout_observed", "call"): "{call}",
    ("no_telemetry_from", "metric:container_cpu"): "s", ("no_telemetry_from", "metric:process_cpu"): "s",
    ("cpu_above_baseline", "metric:container_cpu"): "%", ("cpu_above_baseline", "metric:process_cpu"): "1",
    ("pool_wait_above_baseline", "connect"): "ms", ("pool_wait_above_baseline", "metric:pool_checked_out"): "{connection}",
    ("log_error_pattern", "log"): "{record}",
}


def family(r: dict) -> str:
    if r["modality"] == "trace":
        if r["kind"] == "CLIENT" and r["peer_service"] and r["peer_service"] != r["service"]:
            return "call"
        if r["kind"] == "CLIENT" and r["operation"] == "connect" and r["peer_service"] is None:
            return "connect"
        return "server" if r["kind"] == "SERVER" else "other_span"
    if r["modality"] == "metric":
        return f"metric:{r['measurement']}"
    return r["modality"]


def provenance(r: dict) -> dict:
    """Immutable metadata of a record, as the telemetry recorded it."""
    return {"evidence_id": r["evidence_id"], "emitter": r["service"], "modality": r["modality"],
            "span_kind": r["kind"] if r["modality"] == "trace" else None,
            "callee": r["peer_service"] if family(r) == "call" else None, "operation": r["operation"],
            "measurement": r["measurement"], "unit": r["unit"], "family": family(r)}


def _implied_subject(pred, prov):
    who = ADMISSIBLE.get(pred, {}).get(prov["family"])
    if who is None:
        return None
    return prov["callee"] if who == "callee" else prov["emitter"]


def assess(claim: dict, view) -> dict:
    """Disposition of one original claim, plus the canonical candidate if one is created."""
    cid, pred, kind = claim["claim_id"], claim["predicate"], claim["kind"]
    out = {"claim_id": cid, "predicate": pred, "kind": kind, "original_subject": claim["subject_service"]}
    recs = [view.by_id.get(e) for e in claim["evidence_ids"]]
    out["cited_provenance"] = [provenance(r) if r else {"evidence_id": e, "missing": True}
                               for e, r in zip(claim["evidence_ids"], recs)]
    if kind != "observed" or pred == "onset_before":
        out.update(disposition="not_applicable",
                   reason="inferred claim" if kind != "observed" else "onset_before is relational")
        return out
    if not recs or any(r is None for r in recs):
        out.update(disposition="rejected", reason="a cited record is not in the view" if recs else "no citation")
        return out
    implied = [_implied_subject(pred, p) for p in out["cited_provenance"]]
    if any(s is None for s in implied):
        bad = sorted({p["family"] for p, s in zip(out["cited_provenance"], implied) if s is None})
        out.update(disposition="rejected", reason=f"record family {bad} is not admissible for {pred}")
        return out
    if len(set(implied)) > 1:
        out.update(disposition="rejected", reason=f"cited records imply different subjects {sorted(set(implied))}")
        return out
    subj = implied[0]
    fams = {p["family"] for p in out["cited_provenance"]}
    changes = {}
    if subj != claim["subject_service"]:
        changes["subject_service"] = [claim["subject_service"], subj]
    if claim["unit"] not in VERIFIER_UNITS[pred]:
        units = {CANON_UNIT[(pred, f)] for f in fams}
        if len(units) != 1:
            out.update(disposition="rejected", reason=f"unit {claim['unit']!r} inadmissible and families disagree")
            return out
        changes["unit"] = [claim["unit"], units.pop()]
    # a stray object_service on a non-relational claim is flagged, not transformed: the frozen verifier ignores
    # it for these predicates, so clearing it would create a "canonical" claim identical in substance
    out["stray_object_service"] = claim.get("object_service")
    out["implied_subject"] = subj
    if not changes:
        out.update(disposition="consistent")
        return out
    if claim.get("object_service"):
        changes["object_service"] = [claim["object_service"], None]
    canon = dict(claim)
    for k, (_, new) in changes.items():
        canon[k] = new
    canon["claim_id"] = f"{cid}~canon"
    rules = {"subject_service": "subject := " + ("callee of the cited calls" if fams == {"call"} else
                                                 "emitter of the cited records" if "call" not in fams else
                                                 "provenance-implied subject"),
             "unit": "unit := the unit fixed by the cited record family (value NOT rescaled)",
             "object_service": "object_service := none (only onset_before is relational)"}
    audit = {"transform_version": TRANSFORM_VERSION, "derived_from": cid, "changes": changes,
             "rules": {k: rules[k] for k in changes}, "unchanged": ["predicate", "value", "window", "evidence_ids",
                                                                     "kind", "measurement"],
             "original_sha256": hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()}
    out.update(disposition="transformed", reason=", ".join(sorted(changes)), canonical=canon, audit=audit)
    return out


def canonicalize(diag: dict, view) -> dict:
    """All original claims stay in `diag` untouched; returns the assessments and a separate canonical set."""
    before = json.dumps(diag, sort_keys=True)
    rows = [assess(c, view) for c in diag.get("factual_claims") or []]
    assert json.dumps(diag, sort_keys=True) == before, "originals must never be modified"
    return {"transform_version": TRANSFORM_VERSION, "assessments": rows,
            "canonical_claims": [r["canonical"] for r in rows if r["disposition"] == "transformed"]}
