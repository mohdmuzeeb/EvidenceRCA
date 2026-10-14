"""Versioned prompts for B4 (LLM-only) and E1 (EvidenceRCA ranking + LLM explanation). Agent side.

The instruction text is case-invariant: it is a constant of the prompt version and identical for every case
(tests check this), so it cannot carry case labels. The fault-type names it lists are the task's closed
output vocabulary, the same for every case. All case-dependent text comes from phase4.agent.evidence_pack.
Evidence ids are shown as short aliases (E01, E02, ...) and mapped back deterministically after the call.
"""

import hashlib

from evidencerca.evidence import ALL_SERVICES
from evidencerca.methods.diagnosis import FAULT_TYPES, PREDICATES
from phase4.agent.evidence_pack import Pack, render, render_record

PROMPT_VERSION = "p4a-v1"

SYSTEM = f"""You are a site-reliability engineer doing root-cause analysis for a microservice system with the
services {", ".join(ALL_SERVICES)}. You receive telemetry summaries and a table of citable evidence records
(aliases E01, E02, ...). Use only this evidence; do not assume anything that is not shown.

Return one JSON object:
- decision: "DIAGNOSE" if the evidence identifies a root-cause service, else "ABSTAIN".
- suspected_service: the root-cause service, or "none" when abstaining.
- suspected_fault_type: one of {", ".join(FAULT_TYPES)}, or "unknown".
- ranked_candidates: up to 5 services, most likely root cause first.
- claims: up to 6 factual claims, each {{kind, predicate, subject_service, object_service, value, unit, evidence}}.
  kind "observed" = a measurable fact recomputable from the cited records; kind "inferred" = one causal
  hypothesis (value 0, cite its premises). evidence = 1-5 aliases from the table, never invented.
  object_service is "none" except for onset_before. Predicates and how value is formed:
  * latency_above_baseline (ms): cite the subject's server spans, calls to the subject, or its latency_mean
    metric; value = median of the cited records' values.
  * error_rate_above_baseline (1): cite error server spans of the subject; value = that service's analysis
    error_ratio from the summary. (Or cite its error_ratio metric records; value = their median.)
  * call_failed ({{call}}): cite failed calls to the subject; value = its inbound-call analysis errors count.
  * timeout_observed ({{call}}): cite timed-out calls to the subject; value = its inbound-call timeouts count.
  * no_telemetry_from (s): cite the subject's container_cpu/process_cpu metric records; value = the larger
    max_gap_s of those two streams.
  * cpu_above_baseline (% for container_cpu, 1 for process_cpu): value = median of cited values.
  * pool_wait_above_baseline (ms for connect spans, {{connection}} for pool_checked_out): value = median of cited.
  * log_error_pattern ({{record}}): cite error logs of the subject; value = its analysis error-log count.
  * onset_before (s): the subject's anomaly starts before the object's. Cite one record of the subject and one
    record emitted by the object; value = t(object record) - t(subject record).
  An observed claim must be above the subject's own baseline in the summary; otherwise do not make it.
- propagation_path: up to 4 edges {{from, to, evidence}} from the root cause towards affected callers; "to"
  must call "from" (an observed call edge); evidence = calls made by "to" to "from".
- uncertainty_reasons: up to 3 short strings (missing telemetry, conflicting signals, weak deviation).
- explanation: at most 3 sentences.
ABSTAIN when no service deviates clearly from its baseline or the evidence is insufficient or conflicting."""

TASK_B4 = "Identify the root-cause service from the evidence above."
TASK_E1 = ("EvidenceRCA (a deterministic ranker) produced the ranking above and retrieved the records marked "
           "'retrieved by EvidenceRCA'. Explain the diagnosis of its top candidate with verifiable claims. If the "
           "evidence does not support that candidate, say so in uncertainty_reasons and choose ABSTAIN.")


def aliases(pk: Pack) -> dict:
    """alias -> evidence id (E01.. in table order)."""
    return {f"E{i + 1:02d}": eid for i, eid in enumerate(pk.record_ids)}


def _case_text(pk: Pack, ranking: dict | None) -> str:
    sec = render(pk)
    al = {v: k for k, v in aliases(pk).items()}
    table = "\n".join(f"{al[r['evidence_id']]} [{tag}] " + render_record(r, pk.alert_ns).split(" | ", 1)[1]
                      for _, tag, r in pk.records) or "(none)"
    parts = [sec["header"], "Per-service summary (baseline vs analysis window):\n" + sec["digest"],
             "Observed call edges (caller -> callee):\n" + sec["graph"]]
    if ranking is not None:
        rk = ", ".join(f"{c['service']} ({c['score']:.2f})" for c in ranking["ranked_candidates"][:5]) or "(none)"
        path = ", ".join(f"{e['from']}->{e['to']}" for e in ranking.get("propagation_path", [])) or "(none)"
        parts.append(f"EvidenceRCA ranking (score): {rk}\nEvidenceRCA fault type: "
                     f"{ranking.get('suspected_fault_type') or 'unknown'}; propagation: {path}")
    parts.append("Evidence records (alias [selection] | time | service | type | value | status):\n" + table)
    return "\n\n".join(parts)


def build_messages(pk: Pack, condition: str, ranking: dict | None = None) -> tuple[list[dict], dict]:
    assert condition in ("B4", "E1") and (ranking is not None) == (condition == "E1")
    case_text = _case_text(pk, ranking)
    user = case_text + "\n\n" + (TASK_E1 if condition == "E1" else TASK_B4)
    meta = {"prompt_version": PROMPT_VERSION, "condition": condition,
            "system_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
            "task_sha256": hashlib.sha256((TASK_E1 if condition == "E1" else TASK_B4).encode()).hexdigest(),
            "case_text_sha256": hashlib.sha256(case_text.encode()).hexdigest(),
            "case_text_chars": len(case_text), "system_chars": len(SYSTEM), "n_records": len(pk.records)}
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], meta


def case_text_only(pk: Pack, ranking: dict | None = None) -> str:
    """For leakage tests: the case-dependent text of a prompt."""
    return _case_text(pk, ranking)


__all__ = ["PROMPT_VERSION", "SYSTEM", "build_messages", "aliases", "case_text_only", "PREDICATES"]
