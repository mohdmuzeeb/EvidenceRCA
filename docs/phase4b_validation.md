# Phase 4B validation: execution integrity, reproducibility and deviations

**Plan.** `docs/phase4b_plan.md`, together with `results/phase4b/plan.json` and `phase4/eval/phase4b.py`, was committed at `b5bf532` (2026-10-14) **before any Phase 4B model call**.

**Results.** `docs/phase4b_results.md`.

## 1. Preservation

| Check | Result |
|---|---|
| Freeze manifest (`912085c`) | `verify_frozen()` passed before the plan, before the run and in the test suite |
| Calibration (`fb6a9fd`) and held-out results (`7b54850`) | unchanged; covered by the hash lock below |
| Pre-Phase-4B hash lock (`results/phase4b/preserved_before_phase4b.json`) | 171 files at `00f6777` byte-identical (`tests/test_phase4b.py`). It covers: Phase 4A, 4A-v2 and 4A-v3 code and results; `results/final`; the related docs and tests |
| Earlier locks (`p4a_v1_preserved.json`, `preserved_phase4a_v1_v2.json`) | pass (`tests/test_phase4_v2.py`, `tests/test_phase4_v3.py`) |
| Code changes outside `phase4/eval/phase4b.py`, `tests/test_phase4b.py` and docs | none. E0, B1–B3, verifier, abstention, thresholds, telemetry, p4a-v1 prompt, schema, evidence pack and LLM client are untouched |

## 2. Execution integrity

**Counts and model.**
- **Calls:** 512 planned and 512 completed (B4 265, E1 247). All returned `ok` and parsed schema-valid. There were 0 timeouts, 0 transport failures, 0 retries and 0 truncations at the token limit.
- **Model:** one digest, `359d7dd4bcda…`, and one Ollama version, 0.34.2, throughout.

**Matching the committed plan.**
- **Request hashes:** every call's request hash equalled the hash committed in `plan.json`. The runner aborts on any mismatch, and none occurred.
- **Determinism:** 3 completed calls chosen in advance (seed 20261102: k = 67, 163, 386) were re-executed, and the raw text was byte-identical (`repro_check.json`). These were the only calls beyond the plan.
- **E0 reproduction:** E0 recomputed on all 233 alerted views equals the frozen test output hashes. The E0 reference and E1's diagnosis-level outcomes are therefore the frozen ones.

**Inputs.**
- **Leakage:** the plan-time scan of all 512 prompts found 0 violations.
  - It used the frozen label-token scanner.
  - It also checked for scenario keys, injection timestamps and case ids.
  - As in Phase 4A, E0's own fault-type line in E1 is exempt.
- **Record budget:** B4 and E1 received the same record budget on every view.

**Secondary conditions.**
- **E2:** E1's stored verification was re-verified with the frozen verifier and asserted identical on every view. E2 made no model call.
- **B4-G:** derived from stored B4 outputs and the frozen detector, with no model call. The calls B4-G avoids are reported as a counterfactual.

**Environment.**
- On AC power with Colima stopped throughout; the runner checked both before every call.
- One call at a time, under `caffeinate`.
- Disk ≥ 20 GB free.
- Swap was heavily used (about 8–10 GB of 9–11 GB), but no call failed. Ollama was stopped after the run.
- Snapshots are in `results/phase4b/run_logs/`.

## 3. Statistical design as executed

- **Unit:** the base case is the independent unit (47 faults, 6 controls). Views are repeated measures.
- **Uncertainty intervals:**
  - Wilson intervals are on views, for description only;
  - the cluster bootstrap over base cases uses 10,000 resamples and seed 7, via the frozen `cluster_boot`.
- **Paired tests:** exact McNemar per view spec (one view per base case), with Holm correction across the 5 specs.
- **Control results** rest on 6 base cases and are descriptive only.
- **Framing:** all Phase 4 results are exploratory and post-study.

## 4. Deviations and additions (all disclosed)

1. **Added after the run: paired claim comparison.** `paired.claims_E1_vs_B4` in `condition_comparison.json` gives the per-base-case verified rate and the number of verified root claims, E1 − B4, with a cluster bootstrap.
   - The plan specified per-condition claim rates only.
   - This addition is descriptive and changes no predeclared metric.
2. **Analysis-code robustness fix before the run.** It was found during a no-call smoke test of `analyze` on an empty run, and it changes no definition.
   - The analysis now records views whose planned calls are missing instead of asserting.
   - On the actual run, none were missing.
3. **E1 under B4's window rule.** On the 12 non-alerted control views where E0 still has a candidate, E1 diagnoses, giving 12 control false diagnoses.
   - This was predeclared as the "E1" reading. The frozen protocol reading ("E1-frozen-gated", = E0) is reported alongside it.
   - The E1 calls on those views were made so that B4 and E1 have matched inputs. Their claims are reported as claims on controls.
4. **Audit stratum composition.** E0 has only 59 rejected claims. After the per-case cap, 39 of its 40 rejected audit items are `onset_before`, so predicate balance in that stratum is limited by availability.
   - The other strata meet the ≥ 4 per predicate group target where claims exist.
   - The sample is exactly 80 + 80 + 80, with ≤ 3 items per base case and condition (`audit_manifest.json`).

## 5. Human audit package

| File | Content |
|---|---|
| `results/phase4b/audit_blinded.csv` | 240 items. Same rendering for every condition: canonical claim template, cited records rendered from the telemetry view itself, ≤ 30 same-family context records per window, call edges and the window header. Empty fields `support_label`, `partial_reason`, `evidence_relevance`, `notes` |
| `results/phase4b/audit_key_sealed.json` | Condition, verifier verdict and checks, and ground truth: roots, fault type, and whether the subject is the true root or a symptom service. Annotators must not open it |
| `results/phase4b/audit_manifest.json` | Strata, fill and checksums |
| `results/phase4b/audit_instructions.md` | Rubric, field definitions, independent double annotation, κ / AC1, the adjudication procedure, and the single-annotator fallback |

- **Determinism:** the sample is deterministic (seed 20261101). Regenerating it gave identical CSV and key hashes.
- **Concealment:** whether a claim concerns the true root depends on ground truth. It is therefore recorded in the sealed key and is not an annotator field.
- **Worked examples:** the 10 dev-only examples have not been written. They need human labels, which Claude did not create.
- **Status: PENDING.** No human labels exist and no verifier accuracy is reported. Verifier verdicts are not used as labels.

## 6. Regression suite

**261 passed** (527 s), including the freeze check and all hash locks (`results/phase4b/run_logs/pytest.log`). The tests make no model calls.

## 7. Files

**Results in `results/phase4b/`:**
- `llm_trials.jsonl.gz`: all 512 call records (raw response, parsed output, claims, verifier report, prompt, hashes, tokens, latency), plus diagnosis-level trials for every condition.
- `claim_metrics.json`, `condition_comparison.json`, `runtime_summary.json`.
- `plan.json`, `repro_check.json`, the audit files, and `run_logs/`.

**Raw files.** The per-call raw files (`raw/`, `raw_repro/`, 13 MB) are kept locally and gitignored. Their content is packed into `llm_trials.jsonl.gz`.
