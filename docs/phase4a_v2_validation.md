# Phase 4A-v2 validation: p4a-v1 vs p4a-v2 dev pilot

**Scope.** Development only (dev seed 11). No calibration or test case was opened.
- The rationale, the case-selection rule and the pre-declared reading were committed before any p4a-v2 call (`c87ce77`, `docs/prompt_revision_rationale.md`).
- The frozen study (912085c / fb6a9fd / 7b54850) and every p4a-v1 artifact (eeb18fc) are unchanged. The freeze manifest and the p4a-v1 hash lock both re-verified.
- Phase 4B has **not** been started.

**Verdict.** p4a-v2 does **not** clearly improve claim faithfulness, and it does **not** improve control abstention.
- **What it fixed:** unit misuse (31 → 0 claims), propagation-edge direction (verified 6/67 → 15/44; reversed 37 → 9), and B4's top-1 on fault views (2/16 → 5/16).
- **Overall claim faithfulness is flat.** The verified rate among observed claims went from 27.0 % to 27.4 %, while verified claims per call fell from 1.53 to 1.34.
- **The main failure is unchanged.** Claims that name the caller as subject while citing its CLIENT spans (V3) stayed the most common failure: 106 claims under v1, 113 under v2. This held even though the prompt now explains CLIENT vs SERVER spans and requires each citation's emitter.
- **Control abstention did not improve.** On all four healthy control views, v2 B4 reported `evidence_strength = clear_anomaly` and diagnosed. Its regressions are listed in §5.

## 1. Run

**What ran.**
- 80 planned calls over 20 views × {B4, E1} × {p4a-v1, p4a-v2}. 76 LLM calls were made; 4 E1 trials made no call because E0 had no candidate (both healthy C0 views).
- 76/76 calls returned ok: no timeouts, no model failures, no retries.
- The run took 5274 s in total, of which 5118 s was LLM wall time.

**Model.** qwen3:4b, digest `359d7dd4…74fae7`, on Ollama 0.34.2. Settings were identical for both versions: temperature 0, seed 20261014, num_ctx 6144, num_predict 1024, thinking off.

**Same evidence.** In all 38 called pairs, v1 and v2 received byte-identical case text (`same_evidence_text`).

**Reproducibility.** Two pilot v1 requests were byte-identical to Phase 4A benchmark calls 3 (cascade C0 E1) and 5 (healthy C0 B4). Both returned **identical raw text**: decoding is deterministic.

**Resources.**
- Before the run: AC power, Colima stopped, disk 19 GB free, swap 6.55 / 7.17 GB used, memory-pressure free 48 %.
- Ollama peak RSS 4.08 GB.
- After the run: swap 6.44 / 8.19 GB, disk 20 GB free.
- One inference at a time; Ollama was stopped afterwards. Cost $0.

## 2. Claim-level results (frozen verifier V0–V6, unchanged)

| | v1 B4 | v2 B4 | v1 E1 | v2 E1 |
|---|---|---|---|---|
| Calls (parse valid) | 20 (19) | 20 (20) | 18 (18) | 18 (18) |
| Truncated at 1024 tokens | 1 (healthy control) | 0 | 0 | 0 |
| Observed claims | 113 | 98 | 102 | 88 |
| Verifier-verified | 33 (29.2 %) | 25 (25.5 %) | 25 (24.5 %) | 26 (29.5 %) |
| Verified per call | 1.65 | 1.25 | 1.39 | 1.44 |
| Verified claims about the true root | 2 | 4 | 13 | 5 |
| First failed check: V0 unit/schema | 18 | **0** | 13 | **0** |
| V3 subject association | 52 | 60 | 54 | 53 |
| V4 value | 4 | 12 | 7 | 7 |
| V5 not above baseline | 6 | 1 | 3 | 2 |
| Typed-rejected (v2 only) | – | 61 | – | 62 |
| Post-filter (typed ∧ verified) | 33 | 25 | 25 | 21 |
| Citations with a known alias | 226/226 | 140/140 | 277/277 | 131/131 |
| Declared emitter + record type correct | – | 117/140 | – | 121/131 |
| Propagation edges verified / raw | 1/39 | 10/27 | 5/28 | 5/17 |
| Edges reversed | 28 | 6 | 9 | 3 |
| Verifier flag: DIAGNOSE without a verified suspect claim | 9 | 13 | 12 | 10 |

**Pooled, B4 + E1.**
- Verified rate: 58/215 = 27.0 % (v1) vs 51/186 = 27.4 % (v2).
- Paired by view and condition, v2 had more verified claims in 14 pairs, fewer in 14 and the same number in 10.

**What the typed schema achieved.**
- Grammar-level unit typing removed V0 completely.
- The edge-direction instruction removed most reversals.
- The citation cap (3) shortened answers, so there were fewer claims in total.

**What it did not achieve.**
- **V3 persists.** 79 typed-check reasons on v2 claims are "call X→Y is not a call to the subject". The model still names the caller (often gateway) as the subject of `latency_above_baseline` while citing gateway→Y call records. In 26 citations it also declared the wrong emitter.
- **V4 got worse for B4** (4 → 12). The "copy the middle of 3 values" rule was not followed reliably. Display precision explains only 2 of v2's V4 failures (`v4_value_matches_rendered_precision`).
- **E1 under v2 made fewer verified claims about the true root** (13 → 5). It wrote more claims about other services (§4).

**Typed checks vs the verifier.**
- 5 claims were typed-rejected but verifier-verified, all in v2 E1. In each, the model declared the wrong emitter on a citation, but the claim's association is correct. The declaration rule is stricter than V3, and it lowered v2 E1's post-filter set from 26 to 21.
- 17 claims passed the typed checks but failed the verifier, on V4/V5 (values and baselines), which the typed checks deliberately do not cover.
- This agreement is expected by construction. It is not evidence that either layer is correct; that requires the human audit.

## 3. Control abstention (`results/phase4a_v2/control_abstention.json`)

There are 4 control views: two healthy dev cases × C0 and R40. The detector did not alert on any of them, so all four use the label-free median-time window.

| | v1 B4 | v2 B4 | v1 E1 (E0 decision / LLM rec.) | v2 E1 |
|---|---|---|---|---|
| C0 healthy-0 | DIAGNOSE (inventory) | DIAGNOSE (inventory) | ABSTAIN, no call | ABSTAIN, no call |
| R40 healthy-0 | DIAGNOSE (gateway) | DIAGNOSE (inventory) | DIAGNOSE / DIAGNOSE | DIAGNOSE / DIAGNOSE |
| C0 healthy-1 | ABSTAIN *(truncated output, not a real abstention)* | DIAGNOSE (inventory) | ABSTAIN, no call | ABSTAIN, no call |
| R40 healthy-1 | DIAGNOSE (inventory) | DIAGNOSE (order) | DIAGNOSE / DIAGNOSE | DIAGNOSE / DIAGNOSE |
| False diagnoses | 3 (+1 malformed) | **4** | 2 | 2 |

**Reading the table.**
- v2 B4 rated every view it answered, all 20 including the 4 controls, as `clear_anomaly`.
- The v2 E1 recommendation was `clear_anomaly` on 17 of 18 views, including both called controls.
- The only ABSTAIN the model produced in the whole pilot was v2 E1 on one fault view. That was over-abstention: a fault was present.
- The qualitative abstention instruction and the `evidence_strength` field did not change qwen3:4b's behaviour on controls.
- E1's correct abstentions on the two healthy C0 views come from E0 having no candidate, not from the LLM.

## 4. Diagnosis-level results (fault views: 16 per condition)

| | v1 B4 | v2 B4 | v1 E1 | v2 E1 |
|---|---|---|---|---|
| Top-1, diagnosis level (E1 = E0's) | 2 | 5 | 10 | 10 |
| LLM's own top-1 | 2 | 5 | 10 | 7 |
| LLM fault-type correct | 4 | 4 | 7 | 6 |
| LLM own ABSTAIN on a fault view | 0 | 0 | 0 | 1 |
| E1 LLM disagrees with E0 top-1 | – | – | 2 | 4 |

**B4.**
- B4's gain comes from the missing-trace and conflicting views, where it now names order.
- B4's suspect is still inventory in 12 of 20 v2 answers, versus 13 of 20 under v1. This is a strong prior that is unrelated to the evidence.

**E1.**
- E1's diagnosis-level outcome is E0's by construction, so it is unchanged.
- The v2 model disagreed with E0 more often (4 vs 2 views), and those disagreements made it less often right.

## 5. Regressions (reported as pre-declared)

- **Control false diagnoses:** v2 B4 diagnosed 4/4 controls; v1 B4 diagnosed 3/4, and its fourth answer was a truncated, malformed output, not a real abstention. E1 recommendations: 2/2 called controls under both versions. **There is no improvement.**
- **Latency and tokens:**
  - B4 mean wall time rose from 66.4 s to 76.8 s; E1 from 58.5 s to 66.7 s.
  - Prompt tokens rose by about 550 (the system prompt grew from 2.9k to 6.0k characters).
  - Output tokens rose by 76–113. Peak prompt was 4515 tokens, so prompt plus num_predict stays under num_ctx.
- **Verified claims per call** fell for B4 (1.65 → 1.25).
- **V4 value errors** rose for B4 (4 → 12).
- **E1:**
  - fewer verified claims about the true root (13 → 5);
  - lower LLM-own top-1 (10 → 7);
  - one over-abstention on a fault view.
- **Typed checks** rejected 5 claims that the verifier verified (declared-emitter mistakes only).

**Not regressions.**
- Schema compliance was 38/38 valid under v2, against 37/38 under v1, whose one failure was a truncation.
- Citation validity was 100 % in all four groups.

## 6. Limitations

- **Small sample.**
  - n = 20 views, of which only 7 are base cases. The four cascade views all come from the one dev cascading case.
  - Each version ran once at temperature 0 (deterministic, so repeats would not add information).
  - No significance test was run; all differences are descriptive.
- **Two changes are confounded.** v2 bundles several changes: typing, ownership, direction, abstention and citation caps. The pilot cannot attribute effects to individual changes.
- **Verifier verdicts are not ground truth.** They are the frozen verifier's. The verified rates above measure agreement with V0–V6, not human-judged support.

## 7. Human audit preparation

**Dev dry-run sheet.** `results/phase4a_v2/audit/audit_sheet_dev_pilot.csv` holds **200 claims**:
- 50 per {v1, v2} × {B4, E1} stratum;
- each stratum split 25 verifier-verified / 25 rejected, with no shortfall;
- predicate groups: latency/cpu/pool 151, errors/calls/timeouts/logs 40, onset 4, the rest inferred or silence;
- seed 20261015.

**Blinding.**
- Each item shows a canonical rendering of the claim, the cited records as the model saw them (selection tags removed), up to 30 same-family baseline and analysis records, and the call edges.
- Hidden: version, condition, the model's wording, verifier and typed verdicts, and the ids.
- The key is in `audit_key_dev_pilot.SEALED.json`.

**Purpose.** The sheet is for rubric piloting and dev worked examples. It is **not** the Phase 4B sample.

**Phase 4B plan.** The Phase 4B audit sampling plan was recorded before Phase 4B as `docs/llm_evaluation_protocol.md` §9.1: seed 20261101, 80 per condition (or per condition × version), 40/40 by verdict, ≥ 4 per predicate group, ≤ 3 per base case.

**No human labels exist yet.** The verifier's accuracy is therefore **not** claimed.

## 8. Implications for Phase 4B (for the user's decision; nothing is changed now)

- **Neither prompt makes qwen3:4b abstain on healthy views.** A B4 abstention analysis in Phase 4B will therefore mostly measure false diagnoses. Options:
  - accept that as a finding;
  - try a different model size (that needs a new download approval);
  - restructure the task, for example by asking first whether *any* service deviates, as a separate call. That would be a further dev iteration.
- **Subject attribution is the dominant faithfulness failure.** Instructions and declared ownership did not fix it in a 4B model. A deterministic alternative within the rules: let the agent-side code fill the subject from the cited records instead of asking the model. That would be a new method variant, E1-T, to be pre-registered and evaluated separately; it is not done here.
- **What p4a-v2 offers if chosen for Phase 4B:** zero unit errors and better edges.
- **What it costs:** about 15 % more latency and lower E1 root-focused claims.
- **Matrix estimate.** The Phase 4B matrix (≤ 470 calls) would take about 8.5 h with v2 (warm-call mean +15 %), or about 7.4 h with v1. Running both versions roughly doubles that.

## 9. Files

- `results/phase4a_v2/dev_comparison.json`: per group and category summaries, per-pair comparison, reproducibility, resources.
- `results/phase4a_v2/claim_analysis.json`: every claim and edge, with verifier checks and notes, typed-check reasons and failure classes.
- `results/phase4a_v2/control_abstention.json`: every trial's decision, LLM recommendation and evidence_strength.
- `results/phase4a_v2/raw/`: 80 per-trial files, each with the call record, raw model text, user message and full output.
- `results/phase4a_v2/pilot_plan.json`, `structured_output_tests_v2.json`, `p4a_v1_preserved.json`, `audit/`, `run_logs/`.
