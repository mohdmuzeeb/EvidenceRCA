# Claim audit: guide for annotators

Thank you for taking part. You will judge whether **240 short factual claims about a microservice incident** are supported by the telemetry records they cite. This takes about **6–8 hours**, and it can be spread over several sessions.

Please work **alone**. Do not discuss items with the other annotator until you have both submitted your labels.

## What you receive

Your packet folder `annotator_<ID>/` contains:

| File | Use |
|---|---|
| `items.html` | Open in any browser. It shows the 10 worked examples and then the 240 items, one section per item. This is the easiest way to read the items |
| `labels_<ID>.csv` | **The file you fill in and return.** It has one row per item, in the same order as `items.html` |
| `audit_blinded.csv` | The same 240 items as a spreadsheet, if you prefer reading them there. Do not edit its item columns |
| `worked_examples.csv` | The 10 worked examples with their labels and explanations |
| `packet_manifest.json` | Checksums. You don't need it |

You will **not** receive any information about which system wrote a claim, whether an automatic checker accepted it, or what the true fault was. That is deliberate. Please don't try to find out, and tell the study lead if anything in the packet seems to reveal it.

## What each item shows

- **Claim:** a fixed template, `predicate(subject=…, object=…) = value unit`.
  - `INFERRED` claims are causal hypotheses. Judge only whether the cited premises are real and relevant. Never treat causation itself as established.
- **Windows:** the baseline window and the analysis window, in seconds relative to the window anchor (t = 0). The record times use the same reference.
- **Cited records:** the telemetry records the claim cites. `NOT IN THE TELEMETRY VIEW` means the cited record does not exist.
- **Observed call edges:** caller → callee pairs seen in the traces.
- **Baseline / analysis context:** up to 30 records of the same family (for example, the same span type) from each window. Compare against them to decide whether a value is unusual.

**Precision.** Values are shown at a fixed precision: 0.1 ms for spans and 4 significant digits for metrics. Judge values at that precision.

**Service ownership.** A `CLIENT` span written `A -> B` is emitted by A and measures A's calls to B. For latency, failed calls and timeouts it therefore describes **B as seen by its callers**, not A's own processing.

## How to label

In `labels_<ID>.csv`, fill in these columns for every row. Do not change `audit_id`, and do not add, delete or reorder rows.

**1. `support_label`.** Exactly one value, typed exactly as shown:

| Value | Use it when |
|---|---|
| `supported` | The cited records, read against the baseline context, establish the claim as stated. The value is within 5 % (or the stated tolerance), and the direction of the deviation is clear |
| `partially_supported` | The direction or predicate holds, but the value, unit, window or object is wrong, or only some of the cited records support it |
| `unsupported` | The cited records do not show the claim, contradict it, belong to another service, or do not exist |
| `not_assessable` | The shown evidence is not enough for you to decide |

**2. `partial_reason`.** Fill this in **only** when the label is `partially_supported`. Use one of: `value`, `unit`, `window`, `object`, `subset_of_citations`, `other`. If you use `other`, explain in `notes`.

**3. `evidence_relevance`.** Do the cited records bear on the claim's subject and measurement? One of:

| Value | Meaning |
|---|---|
| `relevant` | They do |
| `weakly_relevant` | Same service, but a different measure, or only indirectly |
| `irrelevant` | Another service or measure, unrelated |

You may leave it empty only when `support_label` is `not_assessable`.

**4. `notes`.** Optional free text. Please add a short note whenever you were unsure.

**Before you start,** read all 10 worked examples at the top of `items.html`. They come from a separate development data set and are not part of the audit.

**Tips:**
- Judge only what is shown. Do not use outside knowledge of how the system "should" behave.
- When unsure between two labels, choose the less supportive one and write a note. Use `not_assessable` only when the evidence genuinely cannot decide it.
- Take breaks. Label quality drops after about an hour.

## Submitting

1. Save `labels_<ID>.csv` as CSV, UTF-8 if offered. Excel, LibreOffice and Numbers all work, and the checker accepts comma, semicolon or tab delimiters.
2. Send the file to the study lead. Do not send it to the other annotator.
3. The study lead runs an automatic check: every row present, every value valid. If something is wrong you will get the list back, and you may fix only the listed rows.
4. Once accepted, your file is stored unchanged as your pre-adjudication labels. You will not be asked to change accepted labels except in the adjudication meeting below.

## Adjudication (after both annotators have submitted)

- You will meet with the other annotator, and the study lead as facilitator, to go through the items where your labels differ. You will get a sheet listing only those items, with both labels side by side.
- For each item, look at the evidence together and agree on one label. You still will not see which system wrote the claim, the checker's verdict or the true fault.
- If you cannot agree, a third annotator decides when one is available. Otherwise the item is recorded as `unresolved`, and that is reported.
- Agreement statistics are computed from your original submissions, before adjudication. Adjudication does not change them.
