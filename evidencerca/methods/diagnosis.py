"""Diagnosis JSON (protocol §6.1) shared by every method. Methods without a field emit None."""

PREDICATES = ("latency_above_baseline", "error_rate_above_baseline", "no_telemetry_from", "cpu_above_baseline",
              "pool_wait_above_baseline", "log_error_pattern", "call_failed", "timeout_observed", "onset_before")
FAULT_TYPES = ("service_crash", "processing_latency", "network_timeout", "db_pool_exhaustion", "cpu_pressure",
               "cascading_failure")
CLAIM_KEYS = {"claim_id", "kind", "subject_service", "object_service", "predicate", "measurement", "value", "unit",
              "window", "evidence_ids"}


def empty_diagnosis(view, method: str, windows) -> dict:
    return {"case_id": view.case_id, "view_id": view.view_id, "method": method,
            "windows": windows.as_dict() if windows else None,
            "ranked_candidates": [], "suspected_service": None, "suspected_operation": None,
            "suspected_fault_type": None, "evidence_ids": [], "factual_claims": [], "propagation_path": [],
            "missing_modalities": sorted(view.unavailable), "uncertainty_reasons": [], "decision": "ABSTAIN"}


def finish(diag: dict, scores: dict) -> dict:
    """Fill ranked_candidates (descending score, ties by name for determinism) and suspected_service."""
    ranked = sorted(((s, float(v)) for s, v in scores.items()), key=lambda x: (-x[1], x[0]))
    diag["ranked_candidates"] = [{"service": s, "score": round(v, 6)} for s, v in ranked]
    if ranked and ranked[0][1] > 0:
        diag["suspected_service"] = ranked[0][0]
        diag["decision"] = "DIAGNOSE"
    else:
        diag["uncertainty_reasons"].append("no candidate with a positive score")
    return diag


def claim(cid, kind, subject, predicate, value, unit, window, evidence_ids, measurement=None, obj=None) -> dict:
    assert predicate in PREDICATES and kind in ("observed", "inferred")
    return {"claim_id": cid, "kind": kind, "subject_service": subject, "object_service": obj,
            "predicate": predicate, "measurement": measurement, "value": value, "unit": unit,
            "window": list(window), "evidence_ids": list(evidence_ids)}
