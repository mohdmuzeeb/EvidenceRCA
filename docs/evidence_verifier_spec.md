# Evidence Verifier Specification (V0–V6)

The implementation is `evidencerca/verifier.py`. Tests: `tests/test_verifier.py` (synthetic, known-bad claims) and `evaluation/verifier_tests.py`, which writes `results/phase3/verifier_tests.json` (mutations of real dev claims). This document implements protocol §6.

## 1. Scope and independence
- **Inputs:** one Diagnosis JSON and the View it was produced on. Nothing else: no ground truth, no fault labels, no injection metadata, and no pipeline internals.
- **Independence from the pipeline:** the verifier imports no `evidencerca.*` module and no `harness`/`evaluation` code (enforced by a test). It re-derives every quantity with its own code:
  - the baseline window;
  - robust statistics;
  - record association;
  - call edges;
  - onsets;
  - periodic-stream gaps.
- **What it decides:** whether each observed claim is supported by the view's evidence. It does **not** decide whether a causal hypothesis is true. A valid citation is not proof of causation (§4).

## 2. Claim schema (closed vocabulary)

```json
{"claim_id": "c1", "kind": "observed" | "inferred",
 "subject_service": "inventory", "object_service": null | "<service>",
 "predicate": "<one of 9>", "measurement": "span_duration" | ..., "value": 523.8, "unit": "ms",
 "window": [t0_ns, t1_ns], "evidence_ids": ["ev-...", ...]}
```

`object_service` is an extension used only by the relational predicate `onset_before`. Allowed units per predicate:

| Predicate | Units | Value means | Cited records must be (V3) |
|---|---|---|---|
| `latency_above_baseline` | ms, s | median of the cited values | SERVER spans of the subject, CLIENT spans to the subject, or the subject's `latency_mean` metric |
| `error_rate_above_baseline` | 1, % | error fraction of the subject's SERVER spans in the window (or the median of cited `error_ratio` metrics) | SERVER spans of the subject, or its `error_ratio` metric |
| `call_failed` | {call} | number of failed calls to the subject in the window | CLIENT spans to the subject |
| `timeout_observed` | {call} | number of calls to the subject with a timeout error | CLIENT spans to the subject |
| `no_telemetry_from` | s, ms | largest gap of the subject's periodic streams in the window | periodic metrics (`container_cpu`, `process_cpu`) of the subject |
| `cpu_above_baseline` | %, 1 | median of the cited values | `container_cpu` / `process_cpu` of the subject |
| `pool_wait_above_baseline` | {connection}, ms | median of the cited values | the subject's `pool_checked_out` metric, or its local DB `connect` spans |
| `log_error_pattern` | {record} | number of the subject's ERROR+ log records in the window | logs of the subject |
| `onset_before` | s, ms | lead of the object's first cited record over the subject's | at least one record of the subject and one of the object |

## 3. Checks (each pass / fail / not_applicable)

| Check | Rule |
|---|---|
| **V0 schema** | Required keys present. `predicate` is in the vocabulary, `kind` ∈ {observed, inferred}. For observed claims: a known unit, a non-null value, and `object_service` present for `onset_before`. |
| **V1 exists** | Every cited `evidence_id` is in the current view's index (a missing or dropped record fails). There must be at least one citation. |
| **V2 identity/time** | diag.case_id = view.case_id, diag.view_id = view.view_id, and every cited record has that case_id. Every cited record's timestamp lies in [w0 − tol, w1 + tol], where tol = the view's time tolerance (0, J1 = 0.5 s, J2 = 2 s). For `no_telemetry_from` the lower bound extends 90 s back, so the last records before a gap can be cited. |
| **V3 association** | Every cited record has the modality, measurement and service relation in the table above. For inferred claims, every premise must belong to the subject (as emitter or callee). |
| **V4 value/unit** | The value is converted to the canonical unit and compared with the verifier's recomputation, within **5 % relative**. The recomputation is over the cited records (medians, the onset lead) or over the whole view in the window (rates, counts, gaps). For `onset_before`, the lead between the cited records must match within 1 ms + tol. |
| **V5 support** | The predicate holds against a baseline the verifier computes itself. Baseline window = [w0 − 90 s, w0), skipping the first 30 s of the view. Per predicate: |
| | • latency / cpu / pool / `error_ratio` metric: **every** cited record > median + 3·(1.4826·MAD) of the same family (modality, measurement, kind, connect flag) for the subject in the baseline, with at least 3 baseline records. Otherwise it fails as "insufficient baseline". |
| | • `error_rate_above_baseline` (spans): window error fraction ≥ baseline fraction + 0.05, and every cited span is an error. |
| | • `call_failed` / `timeout_observed`: window failure (or timeout) fraction of calls to the subject ≥ baseline + 0.05, and every cited span matches. |
| | • `log_error_pattern`: every cited record is ERROR+, and the window count > the baseline count scaled to the window length, + 2. |
| | • `no_telemetry_from`: every periodic stream of the subject must have an **expected-stream baseline** (≥ 6 records in the baseline; cadence D = median interval), and every such stream has a gap ≥ 3·D in the window. A single missing record therefore never verifies, and a stream without a baseline cadence fails. |
| | • `onset_before`: the verifier computes each service's onset itself: the earliest record exceeding its own baseline (latency, cpu, pool), the earliest failed call or error, or the start of a periodic-stream gap. It requires onset(subject) ≤ onset(object) + 5 s + tol. |
| **V6 topology** | For each propagation edge {from: a, to: b} (the anomaly propagates from a to b): the call b → a must exist in the view's observed call graph. The verifier extracts these edges itself from CLIENT span peers and CLIENT→SERVER parent/child pairs. Cited edge evidence must be CLIENT spans b → a. |

Outcome:
- **Observed claims** are *verified* only if every check passes.
- **Inferred claims** are checked for V0–V3 on their premises. V4/V5 are `not_applicable`, and `verified` is always false. They are reported as `premises_valid` and counted separately.
- **Failed claims are rejected and reported, never rewritten.**
- A DIAGNOSE with no verified observed claim about the suspected service is **flagged**.

## 4. What the verifier does not claim
- A verified observed claim means "this observation is in the evidence", not "this service is the root cause".
- "No telemetry" is never inferred from absence alone, only against an expected-stream baseline.
- The verifier's verdicts are **not** used as ground truth for its own accuracy (§5).

## 5. Evaluating the verifier

### 5.1 By construction (Phase 3, done; `results/phase3/verifier_tests.json`)
Real EvidenceRCA claims from the dev cases are corrupted in known ways. The correct outcome, and the check that must fail, is known without consulting the verifier:

| Mutation | Must fail |
|---|---|
| False citation (a non-existent id) | V1 |
| Cited record removed from the view | V1 |
| Window moved outside the data | V2 |
| Subject changed to an unrelated service | V3 |
| Value ×3 + 1 (or +10 s for onset) | V4 |
| Unit swapped without rescaling | V4 |
| Contradictory evidence: cite the lowest values (or successful calls) of the same family, with the value set consistently | V5 |
| Fabricated propagation edge to a service that never calls the root | V6 |
| "No telemetry" for a healthy service with one record removed | V5 |

### 5.2 Independent human audit (required before the paper; **not yet performed**)
This addresses the verifier's precision and recall on *real, unmodified* claims, which mutation tests cannot measure.
- **Sample:** ≥ 200 observed claims from the test-seed runs, stratified by method (E1, B4, and E0 for contrast), condition (C0, R40, MT/ML/MM, CF) and verifier verdict (pass/fail). Sampling uses a fixed seed.
- **Blinding:** the annotator sees the claim (predicate, subject, value, unit, window) and the cited records, rendered as plain tables with nearby context records from the same view. The method name and the verifier verdict are **hidden**.
- **Rubric:**
  - *Supported*: the cited records show the stated observation, with the stated service, value within 5 %, and window.
  - *Unsupported*: any of these fails, or the observation contradicts the context.
  - *Unclear*: insufficient information; reported separately and excluded from κ.
  - Causation is **not** judged.
- **Reporting:** verifier precision and recall against human labels, Cohen's κ, and inter-annotator κ if a second annotator is available.
- **Tooling:** `evaluation/audit_sample.py` produces the blinded sheet. The labels are entered by a person, never generated.
- **Circularity guard (protocol §6.3):**
  - report unsupported-claim rates before and after filtering;
  - report verified claims per DIAGNOSE.
