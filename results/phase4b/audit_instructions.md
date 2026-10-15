# Phase 4B blinded claim audit: instructions for annotators

**Status:** PENDING. No human labels exist yet.

**Protocol:** `docs/llm_evaluation_protocol.md` §9 and §9.1. The sampling seed is 20261101.

## Files

| File | Who may open it | What it is |
|---|---|---|
| `audit_blinded.csv` | annotators | 240 items, one row per claim |
| `audit_key_sealed.json` | the study lead, after adjudication | condition, verifier verdict and ground truth for each item. Annotators must not open it |
| `audit_manifest.json` | anyone | strata and checksums, with no per-item verdicts |

## What each item shows

- **`claim`:** a factual claim in a fixed template, `predicate(subject=…, object=…) = value unit`.
  - An `INFERRED` item is a causal hypothesis. Judge only whether its cited premises are real and relevant. Never treat causation as established.
- **`cited_records`:** the telemetry records the claim cites, as they exist in the telemetry view. A line reading `NOT IN THE TELEMETRY VIEW` means the cited record does not exist.
- **`windows`:** the baseline and analysis windows, relative to the window anchor. Times in the records use the same reference.
- **Context records:**
  - `context_baseline_same_family`: up to 30 records of the same family in the baseline window, sampled with a fixed seed;
  - `context_analysis_same_family`: up to 30 such records in the analysis window.
- **`observed_call_edges`:** caller → callee edges seen in the traces.

Values are displayed at the evidence pack's precision: 0.1 ms for spans, 4 significant digits for metrics. Judge values at that precision.

## Fields to fill in

**1. `support_label`.** Exactly one of:

| Label | Use it when |
|---|---|
| `supported` | The cited records, read against the baseline context, establish the claim as stated. The value is within 5 % or the stated tolerance, and the direction of the deviation is clear |
| `partially_supported` | The direction or predicate holds, but the value, unit, window or object is wrong, or only some of the cited records support it |
| `unsupported` | The cited records do not show the claim, contradict it, belong to another service, or do not exist |
| `not_assessable` | The shown evidence is not enough for a human to decide |

**2. `partial_reason`.** Fill in only for `partially_supported`: value, unit, window, object, subset of citations, or other.

**3. `evidence_relevance`.** Do the cited records bear on the claim's subject and measurement? Exactly one of:

| Value | Meaning |
|---|---|
| `relevant` | they do |
| `weakly_relevant` | the same service, but a different measure, or only indirectly |
| `irrelevant` | another service or measure, unrelated |

**4. `notes`.** Free text.

**Service ownership.** A `CLIENT` span written `A -> B` is emitted by A and measures A's calls to B. For latency, failed calls and timeouts it therefore concerns **B as seen by its callers**. It is not about A's own processing.

## Procedure

**Annotation.**
1. Two annotators label independently on separate copies of the CSV, without discussing items.
2. Before labelling, read the 10 worked examples.
   - They come only from the dev dry-run sheet (`results/phase4a_v2/audit/`), never from this sample.
   - **They have not been written yet.** The study lead labels and explains them. Claude did not create them, because it must not produce human labels.
3. Each annotator submits their file. Pre-adjudication labels are kept unchanged.

**Agreement.** Report:
- Cohen's κ, unweighted and weighted with the order unsupported < partially_supported < supported;
- Gwet's AC1;
- for `evidence_relevance`, κ separately.

**Adjudication.**
- Every disagreement is discussed by the two annotators with the item, and still without the key.
- If they cannot agree, a third annotator decides when one is available. Otherwise the item is recorded as `unresolved` and reported.
- The adjudicated label is the reference.
- Only after adjudication is the key unsealed for the analyses in §9: verifier precision and recall, per-condition hallucination rates, and the verified-claim error rate.

**Single annotator.** If only one annotator is available, report this. Re-label a random 20 % of items (seeded) at least two weeks later and report the intra-rater κ.

## What must never happen

- The verifier's verdicts are never copied in as labels.
- No item is labelled with knowledge of the condition, the verdict or the ground truth.
