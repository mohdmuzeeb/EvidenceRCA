# LLM Evaluation Protocol (Phase 4: B4 and E1)

Status: **draft for approval**. It was written in Phase 4A (2026-10-14) after the deterministic study was frozen and evaluated.
- Nothing here changes the frozen study:
  - the freeze commit `912085c` (protocol, code, thresholds);
  - the calibration commit `fb6a9fd`;
  - the held-out results in `docs/final_results.md`.
- Phase 4B (the full LLM evaluation) has **not** started and needs your approval.

## 1. Position relative to the frozen study

- **Post-study.** B4 and E1 were not part of the frozen evaluation. They are evaluated separately, and the paper reports them as post-study conditions.
- **Separate code.** It lives in `phase4/` on branch `phase4a`. It imports the frozen modules read-only:
  - the View;
  - the alert detector and windows;
  - E0;
  - the verifier V0–V6;
  - the abstention score S.

  `tests/test_phase4.py` re-verifies the freeze manifest on every run.
- **Unchanged frozen pieces.** No frozen threshold, verifier rule, abstention weight or τ changes. The frozen τ (calibrated for E0 on seeds 23/37) is **not** applied to B4 or E1. If an LLM-specific gate is wanted, see §7.
- **Already-viewed test seeds.** Seeds 41/53/67 have been viewed for E0, so nothing in Phase 4 may be tuned on them (§6).

## 2. Conditions

| ID | What the LLM does | Diagnosis-level outcome comes from | Claims come from |
|---|---|---|---|
| **B4** (LLM-only) | Reads the neutral evidence pack; returns decision, suspected service, ranking (≤5), fault type, claims (≤6), propagation path (≤4), uncertainty reasons, explanation | the LLM | the LLM |
| **E1** (EvidenceRCA + LLM, post-study) | Reads the same pack, whose record table starts with E0's retrieved records, plus E0's ranking; writes claims, path and explanation for E0's top candidate | **E0** (ranking, suspected service and fault type, decision). The LLM's own pick and decision are kept as `llm.*` and never override it | the LLM |
| E0 (reference) | — | frozen | frozen templates |

- **Why E1 does not re-rank.** The approved scope is "candidate ranking and evidence retrieval followed by LLM explanation generation". E1 therefore isolates the LLM's contribution to claims and explanations. Its localization accuracy is E0's by construction, and the paper must say so.
- **Optional E1-R** (not built). The LLM may override E0's top candidate. This would be a separate condition and needs your decision.
- **No alert means no call.** When the shared alert detector does not fire, no window exists and nothing is sent to the model. This applies to both conditions, and the decision is ABSTAIN, as for every frozen method.
- **E0 has no candidate.** E1 also makes no call when E0 has no positive-score candidate.

## 3. Identical inputs and settings

- **Evidence.** `phase4/agent/evidence_pack.py` is method-neutral and identical for every service. It contains:
  - **Header:** window bounds relative to the anchor, and the unavailable modalities.
  - **Digest:** per service and record family, the counts, medians, p95/max, errors, timeouts and error-severity logs for the baseline and analysis windows, plus the maximum metric inter-sample gap.
  - **Call graph:** the observed call edges.
  - **Record table:** at most 36 records. The neutral selection takes, per family, the analysis-window maximum, the first error and the first record above the family's own baseline median + 3·MAD, with services interleaved round-robin.

  E1's table holds E0's retrieved records first and is filled with the neutral selection to **the same size** as B4's. A test checks that B4 and E1 always get equal record counts (84 dev prompts).
- **Aliases.** Evidence ids appear as aliases (`E01`…). The mapping back to `ev-…` ids is deterministic. An alias the model invents is kept verbatim, so V1 rejects it.
- **Leakage.** The prompt contains no case id, no absolute timestamp, no scenario key and no injection time. The instruction text is a constant of the prompt version and is identical for every case. The case-dependent text is scanned with the frozen label-token scanner (`harness.leakage.scan_tokens`) on every dev case. One exclusion: in E1, the line holding E0's own predicted fault type is not scanned, because it is an output of the frozen agent-side E0, not a label. `phase4/agent/` passes the frozen static import/ground-truth check.
- **Model and decoding:**
  - **Model:** qwen3:4b (Q4_K_M, digest `359d7dd4bcda…`) via local Ollama 0.34.2 on 127.0.0.1;
  - **Decoding:** temperature 0, seed 20261014, thinking off;
  - **Limits:** context 6144 tokens, output limit 1024 tokens;
  - **Output format:** JSON schema passed as `format`, i.e. grammar-constrained decoding;
  - **Bounds:** timeout 300 s, at most 1 retry for transport failures only.

  These settings are the same for B4 and E1.
- **Prompt version** `p4a-v1`. It is frozen for Phase 4B once approved. Any change would make a new version, re-run on dev only.

## 4. What is recorded per call

Everything below goes into `results/phase4*/raw/`:
- Model name and digest, Ollama version, every decoding option, the prompt version, hashes of the system, task and case text, and the alias map.
- Each attempt with its outcome.
- Prompt and output token counts, load / prompt-evaluation / generation durations, and wall time.
- `done_reason`.
- The raw response text.
- The parsed object, its schema errors, and its semantic issues.
- The frozen-format diagnosis, the full verifier report, and both claim sets: **pre-filter** (all generated claims) and **post-filter** (verified observed claims).

## 5. Outcome classes and metrics

**Call level.** The outcome classes are mutually exclusive:

| Class | Meaning |
|---|---|
| `timeout` | No response within 300 s on every attempt |
| `model_failure` | HTTP error, server error or connection failure |
| `malformed_json` | Not one JSON object, for example truncated at the token limit (`done_reason = length` is recorded too) |
| `schema_noncompliant` | Valid JSON that breaks the schema |
| `valid` | Schema-compliant |

Semantic issues are counted separately. Examples: an unknown alias, DIAGNOSE with suspected service "none", or `onset_before` without an object.

**Claim level.** Reported separately from diagnosis level:
- Pre-filter claims per call: observed and inferred.
- Citation validity: the share of cited aliases present in the prompt, and present in the view.
- Verifier pass rate per check (V0–V5).
- Verified observed claims per DIAGNOSE.
- Unsupported-claim rate before and after filtering (protocol §6.3.1).
- For inferred claims, premise validity (V1–V3) only. An inferred claim is never counted as a verified fact.
- Rejected claims are reported, never rewritten.

**Diagnosis level:**
- Top-1 and top-3 over triggered trials, and over all fault trials.
- B4 fault-type accuracy, plus the LLM's own fault type for E1.
- The control-diagnosis rate.
- For E1, agreement of the LLM with E0's top-1, and the LLM's own ABSTAIN rate.

**Abstention:**
- **S and A-margin.** Both are reported for E0 exactly as frozen.
- **E1.** The frozen S formula is evaluated with E1's verified-claim coverage, descriptively only. Its RC curve and AURC need no threshold.
- **B4.** It has no scores, so no margin exists. Its abstention is its own DIAGNOSE/ABSTAIN decision, a single operating point with no RC curve. This deviates from protocol §7 ("B1–B4 use their own margin"), and the deviation is logged.

**Cost:**
- wall time per call (p50/p95), load time, tokens per second, and peak Ollama resident memory;
- disk;
- USD 0 (local only).

**Statistics.** As in the frozen protocol §9: Wilson CIs, a cluster bootstrap over base cases, exact McNemar for paired top-1, and Holm within each family.

## 6. Splits and what may be tuned where

| Split | Phase 4 use |
|---|---|
| dev (seed 11) | prompt and pack development, the five-call benchmark, all debugging. **The only split used in Phase 4A** |
| calibration (23, 37) | already used for E0's τ. Usable for an LLM-specific gate only under §7 |
| test (41, 53, 67) | viewed for E0. Phase 4B may *evaluate* frozen B4/E1 on them, but nothing (prompt, pack, budget, gate, verifier) may change after any B4/E1 test output is seen |

If a prompt or pack change turns out to be needed after Phase 4B starts, it is a new version, developed on dev only. Results from both versions are reported.

## 7. An E1-specific abstention score (only if needed)

The frozen S and A-margin stay the primary scores. An E1-specific score, for example one that uses the LLM's own ABSTAIN, its uncertainty reasons, or post-filter claim counts, would need all of the following:
1. A definition written and committed before any data is used to fit it.
2. A **new, disjoint calibration set**. Proposed: fresh capture seeds (for example 79 and 83, never used), captured under the frozen capture protocol.
3. τ fixed on that set, with the same op1/op2 rules as decision 4.
4. Evaluation on held-out data not used for anything else. The current test seeds can carry descriptive curves only, because their E0 results have been seen. A clean held-out claim requires new seeds.

## 8. Proposed Phase 4B matrix (for approval)

- **Core.**
  - Test seeds (47 faults + 6 controls = 53 base cases) × 5 views {C0, R40 (seed 1), MT, ML, CF} × {B4, E1}, which is 530 trials.
  - Controls do not alert at C0, so no call is made for them (protocol §2). Calls happen only for triggered views.
  - E1 makes no call when E0 has no candidate.
  - Expected ≤ 470 calls.
- **Optional:** the same matrix on dev and calibration seeds (more n, descriptive), and A-ver for E1 (free; it reuses outputs).
- **Not included:** paid models (E1-P), RCAEval, dual faults, and the post-study fixes.
- **Time and memory estimates** use the five-call benchmark measurements. They are in `docs/phase4a_validation.md` §5.

## 9. Blinded human audit protocol (≥ 200 claims)

**Goal.** Independent ground truth for claim support, used to estimate the verifier's precision and recall and each condition's hallucination rate. The verifier's own verdicts are **never** used as ground truth.

**Population.** All observed and inferred claims from the evaluated trials of three conditions on the same base cases and views: E0 (frozen test outputs), E1 and B4 (Phase 4B outputs).

**Sampling.** The sample is fixed by a seeded script before any label is entered:
- **Total:** 240 claims, leaving a margin above 200 for not-assessable items.
- **Per condition:** 80 claims each for E0, E1 and B4.
- **Within a condition:** stratified by verifier verdict, with 40 verified and 40 rejected where available, and the shortfall filled from the other stratum and recorded.
- **Predicate groups:** latency/cpu/pool, errors/calls/timeouts/logs, silence, onset, and inferred. Within each verdict stratum the groups are sampled proportionally, with at least 4 per group when available.
- **Per base case:** at most 3 claims, to limit clustering.
- **Order:** shuffled before annotation.

**Blinding.**
- Each claim is rendered in one canonical template from its structured fields: subject, predicate, object, value, unit, window. Annotators never see the LLM's wording, the explanation, the method, the verifier verdict, the case's fault label or any other claim of the same diagnosis.
- Each item shows the cited records, plus context:
  - the subject's records of the same family in the baseline and analysis windows (up to 30 each, sampled with a fixed seed);
  - a plain list of observed call edges.
- No statistic computed by the verifier is shown.
- The key (condition, verdict, stratum) is stored in a separate file that annotators do not open.

**Labels.** Exactly one per claim:

| Label | Definition |
|---|---|
| **supported** | The cited records, read against the shown baseline context, establish the claim as stated (value within 5 % or the stated tolerance; the direction of the deviation is clear) |
| **partially supported** | The direction or predicate holds but the value, unit, window or object is wrong, or only some cited records support it |
| **unsupported** | The cited records do not show what is claimed, contradict it, belong to another service, or do not exist |
| **not assessable** | The shown evidence is insufficient for a human to decide, for example missing context or an ambiguous predicate. These items are reported, and excluded from precision/recall |

- **Inferred claims** are labelled only on whether their premises are relevant and real. Causation is never treated as established.

**Annotators and adjudication.**
- Two annotators label independently, following a written rubric with 10 worked examples taken from **dev** claims only, not from the audit sample.
- Agreement is reported on the four labels with Cohen's κ (unweighted, plus a weighted variant with the order unsupported < partial < supported) and Gwet's AC1. Pre-adjudication labels are kept.
- Disagreements are adjudicated by discussion, or by a third annotator if one is available. The adjudicated label is the reference.
- If only one annotator is available, this is reported. A second pass by the same annotator on a random 20 %, at least two weeks later, gives intra-rater κ.

**Analyses.**
- **Verifier against human labels:** supported = positive, unsupported = negative; partial is analyzed both ways.
- **Precision, recall and κ**, per condition and per predicate group, with bootstrap CIs.
- **Hallucination rate per condition:** unsupported + partial, among pre-filter claims, weighted back to the population with inverse sampling probabilities.
- **Verified-claim error rate:** human-unsupported among verifier-verified claims.

**Tooling** (to be written in Phase 4B):
- `phase4/eval/audit_sampler.py` implements the sampling and blinding above. It writes `audit_sheet.json` and a separate `audit_key_DO_NOT_SHOW_ANNOTATOR.json`. It never produces labels.
- `evaluation/audit_sample.py` is the Phase 3 prototype. It is kept unchanged.

### 9.1 Audit sampling plan, recorded before Phase 4B (added in Phase 4A-v2, 2026-10-14)

This section fixes the Phase 4B audit sample before any Phase 4B output exists. It refines §9 and does not relax it.

**Population.** Every pre-filter claim, observed and inferred, from the Phase 4B trials of three conditions on the same base cases and views:
- **E0:** frozen test outputs, read-only.
- **E1 and B4:** run with the prompt version approved for Phase 4B. If both p4a-v1 and p4a-v2 are run, each version is its own stratum and the per-stratum size below applies to each.

**Size and strata.**
- 80 claims per condition (or per condition × version), so ≥ 240 in total, which leaves a margin above 200 for not-assessable items.
- Within each stratum: 40 verifier-verified and 40 rejected. A shortfall is filled from the other half and recorded.
- At least 4 per predicate group when available. The groups are latency/cpu/pool, errors/calls/timeouts/logs, silence, onset and inferred.
- At most 3 claims per base case and condition.
- Typed-rejected claims (p4a-v2) are eligible like any other claim. Their typed verdict is part of the sealed key only.

**Seed.**
- The Phase 4B sample uses seed 20261101. It is fixed now and is different from the dev dry-run seed 20261015.
- The sampler is `phase4/eval/pilot_v2.py:audit`, generalized to the Phase 4B strata. It adds no label and changes no blinding.

**Blinding.** As in §9 and as implemented in the dev dry run (`results/phase4a_v2/audit/`):
- the claim is shown in a canonical rendering;
- cited records are shown as displayed to the model, with selection tags removed;
- each item shows up to 30 same-family baseline and analysis records, plus the observed call edges;
- the key is sealed.

Displayed values carry the evidence pack's precision (0.1 ms for spans, 4 significant digits for metrics). The rubric tells annotators to judge values at that precision.

**Rubric examples.** The 10 worked examples come from the dev dry-run sheet, never from the Phase 4B sample.

**What is never done.**
- The verifier's verdicts are never used as labels.
- Verifier precision and recall are reported only against adjudicated human labels.
