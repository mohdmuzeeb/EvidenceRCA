# Phase 4B plan (pre-registration): locked evaluation of B4 and E1 on the held-out test seeds

**Status.** This document, `phase4/eval/phase4b.py` and `results/phase4b/plan.json` are committed **before any Phase 4B model call**. Nothing below changes after the first call. Bug fixes to analysis code that do not change a definition are allowed and will be disclosed in `docs/phase4b_validation.md`.

**Prompt engineering has stopped.** p4a-v1 is used unchanged. p4a-v2 is not run in Phase 4B.

## 1. Preservation and disclosure

- **Unchanged frozen study:** freeze commit `912085c`, calibration `fb6a9fd` and held-out results `7b54850`.
  - E0, B1/B2/B3, the verifier V0–V6, the abstention score, τ and the telemetry are not modified.
  - `verify_frozen()` runs before the plan, the run and in the tests.
- **Phase 4A, 4A-v2 and 4A-v3 hash lock.** Their code, results and docs, plus the frozen held-out results, are locked by `results/phase4b/preserved_before_phase4b.json` (171 files at `00f6777`, checked by `tests/test_phase4b.py`). The earlier locks (`p4a_v1_preserved.json`, `preserved_phase4a_v1_v2.json`) stay in force.
- **The held-out set is not untouched.** The deterministic held-out results on test seeds 41/53/67 were inspected in Phase 3/final (E0, B1–B3). The paper must not claim that the whole research process used an untouched holdout.
  - What is new in Phase 4B is the LLM output on these seeds. No B4/E1 output on any test case has been seen before this commit.
  - The prompt, evidence pack, model settings, verifier and every definition below were fixed on dev seed 11 (Phase 4A–4A-v3) and are not changed after any test output is seen.
- **E1's localization is already known.** E1's diagnosis-level result equals E0's on the same window. On alerted views E0's test outputs were seen before this plan, so E1's top-1 is not a new held-out result. Only E1's claims are new.

## 2. Conditions

| | Role | Diagnosis comes from | Claims come from | Model call |
|---|---|---|---|---|
| **B4** | primary | the LLM (p4a-v1), ungated | the LLM | every view (265) |
| **E1** | primary | frozen E0 on the same window (ranking, suspect, fault type, decision) | the LLM (p4a-v1), given E0's ranking and E0's retrieved records first | when E0 has a positive-score candidate (247) |
| **E2** | secondary | E1's (identical by construction) | E1's stored original claims, plus separately counted system-derived canonical claims (`own-v1`) | none (post-processing of E1's stored outputs) |
| **B4-G** | secondary, **hybrid** | the frozen detector gate; on alert, B4's stored output | B4's on alerted views | none (B4's stored calls; silent gate means ABSTAIN) |
| E0 | reference | frozen | frozen templates | none |

- **Information asymmetry.** E1's model sees E0's ranked candidates, scores, fault type and propagation path. B4's does not. Both get the same evidence-record budget, model, decoding settings, schema and window.
- **E1 window and readings.** E1 is evaluated on the same window as B4. E1's diagnosis is reported under two readings:
  - the same window rule as B4: "E1";
  - the frozen protocol, where no alert means ABSTAIN: "E1-frozen-gated". This equals the frozen E0 test outputs.
- **Unusable LLM output.** E1's diagnosis-level outcome stays E0's when the model's output is unusable. Such outputs are counted separately.
- **B4-G is not an LLM-only baseline.** Its abstentions come from the deterministic detector. Its call saving is **counterfactual**: B4 is ungated, so the calls on gate-silent views were made and are counted as calls B4-G would not make.
- **E2's canonical claims are never reported as model-written claims.**

## 3. Matrix (predeclared, coded in `phase4/eval/phase4b.py`)

**Cases.** Every valid test case, `evaluation.run_final.cases_for("test")`: 53 base cases, of which 47 are faults and 6 are healthy controls.
- `41:network_timeout:inventory` is excluded, as in the frozen evaluation (`case_valid = false`).

**Views.**

| Spec | View |
|---|---|
| C0 | clean |
| R40 | 40 % random record loss, perturbation seed 1 |
| MT | traces missing |
| ML | logs missing |
| CF | conflicting decoy service |

That is 265 views, the frozen protocol's evaluation views (`evaluation.views.build`). MT and ML are the missing-telemetry views; R40 is degraded.

**Window rule (label-free, identical for B4 and E1).** Use the shared detector's alert window. If it does not alert, anchor a window at the median record timestamp, as in the Phase 4A-v2/v3 pilots.

**Calls.** 512 in total: B4 265 and E1 247.
- E1 makes no call on 18 control views, where E0 has no candidate.
- The order is view by view in plan order: B4 first on even view indices and E1 first on odd ones.

**Precomputed in `plan.json` (no model):**
- **Detector:** alerts on 233 of 265 views, which is 233/235 fault views and 0/30 control views. The two silent fault views are `41/67:cpu_pressure:payment:strong` under MT.
- **E0 recompute:** 233/233 alerted views reproduce the frozen test output hash.
- **Record budget:** equal for B4 and E1 on every view.
- **Leakage scan:** 0 violations over all 512 prompts. The frozen label-token scanner was run, along with a check for scenario keys, injection timestamps and case ids. E0's own predicted fault-type line in E1 is exempt, as in Phase 4A.
- **Exact request hashes:** the SHA-256 of the full Ollama request body for every one of the 512 calls. At run time each call's hash must equal the planned one, or the run stops.
  - **Prompt length:** estimated at ≤ ~4,100 prompt tokens per call (num_ctx 6144, num_predict 1024). No overflow is expected, and the risk is recorded per call.

**Model and settings (unchanged from Phase 4A).**
- qwen3:4b (digest `359d7dd4…`), local Ollama 0.34.2 on 127.0.0.1;
- temperature 0, seed 20261014, thinking off;
- num_ctx 6144, num_predict 1024;
- JSON-schema `format`;
- 300 s timeout and ≤ 1 transport retry.

There are no paid APIs and no downloads.

**Estimate.**
- Pilot means were 66.4 s per B4 call and 58.5 s per E1 call. That gives 265 × 66.4 + 247 × 58.5 ≈ 32,000 s ≈ **8.9 h**.
- Plausible range: 8–10 h, plus 3 determinism re-executions (~4 min).
- Disk use is about 30 MB for raw records.

## 4. Execution safeguards

- **Before every call:** AC power, Colima stopped and ≥ 5 GB free disk. If any check fails, the run stops cleanly and can be resumed.
- **Concurrency:** one model call at a time (`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`), with `caffeinate`.
- **Per call** (`results/phase4b/raw/NNN_vIII_SPEC_COND.json`, written atomically):
  - **Model and request:** model digest, Ollama version, settings, prompt version, and the system, task and case-text hashes. Also the planned and actual request hash.
  - **Response:** the raw response, the parse status, the structured claims, the frozen-format diagnosis and the full verifier report.
  - **Runtime:** tokens, latency (load, prompt evaluation, generation, wall), `done_reason` and attempts. Ollama peak RSS and system memory/swap after the call.
- **Checkpointing:**
  - A completed call file is never re-run.
  - A call that ended in a transport failure (timeout or model failure) is archived under `raw/failed/` and re-attempted once on a later resume.
  - Malformed or non-compliant answers are results and are never retried.
- **Determinism check (predeclared).** After the run, 3 completed calls chosen with seed 20261102 are re-executed. The raw texts are compared, with the result in `results/phase4b/repro_check.json`. These are the only extra calls.

## 5. Metrics (per condition; base case is the independent unit)

**Diagnosis level.**
- **Top-1:** DIAGNOSE with a true root as the suspect, over all fault views. Also reported: top-3 and fault-type accuracy over all fault views.
- **Other outcomes:** wrong-service diagnoses, abstention on fault views, and false diagnoses on the 30 control views (6 base cases).
- **Breakdowns:** overall, per spec (including MT/ML missing telemetry), and per base case.

**Claim level.** Frozen verifier verdicts, which are **not** factual correctness:
- **Volume and retention:** observed and inferred claims; verified and rejected observed claims before filtering; retention after filtering (verified observed / all claims); verified claims per call.
- **Root relevance:**
  - verified claims about a ground-truth primary root;
  - *off-target* claims, meaning the subject is neither a root nor a ground-truth propagated-symptom service.
- **Citations:** whether all cited aliases are known and all ids are in the view, versus verifier support (valid ids but rejected).
- **Error classes:** ownership (V3), values (V4, with the rendered-precision artifact counted separately), units/schema (V0), unknown citations (V1), out-of-window (V2), not above baseline (V5).
- **Propagation:** edges verified, incorrect and reversed.
- **Breakdowns:** by predicate, by spec, and for fault views vs controls.

**E2.**
- dispositions (consistent / transformed / rejected / not applicable);
- transformation and rejection rates;
- subject-ownership errors and unit changes;
- canonical faithfulness, and canonical claims about the true root;
- verified per call, original only and original + canonical.

**B4-G.**
- gate coverage of fault views and the missed-fault rate, overall and per spec;
- control views alerted;
- counterfactual calls avoided (on controls and on fault views), LLM seconds avoided, B4 correct diagnoses lost to the gate, and B4 false diagnoses on controls removed.

**Abstention.**
- **B4:** its own DIAGNOSE/ABSTAIN at a single operating point. There is no confidence score and no risk-coverage curve.
- **E1:** the LLM's own ABSTAIN recommendation is reported descriptively.
- **E0:** the frozen S and A-margin results, plus the margin-only ablation, are cited unchanged from `results/final/test_results.json`.
- No new abstention score is fitted.

**Runtime.** Per condition:
- wall time (mean, p50, p95, max, total), prompt and output tokens, tokens per second and load time;
- outcomes and parse classes, and truncation at the token limit;
- Ollama peak RSS, transport retries, and cost (USD 0).

## 6. Statistics

- **Independence.** The base case is the independent unit; views are repeated measures of it.
- **Top-1 per condition:** Wilson 95 % on views (descriptive), plus the per-case mean with a 95 % cluster bootstrap over base cases. This uses the frozen `cluster_boot` function: 10,000 resamples, seed 7.
- **Paired comparisons on matched fault views**, for E1 vs B4, E1-frozen-gated vs B4 and B4-G vs B4:
  - exact McNemar per spec (one view per base case per spec), with Holm correction across the 5 specs;
  - the pooled per-case mean difference with a cluster-bootstrap CI.

  Fault type is compared the same way for E1 vs B4.
- **Claim verified rate:** the ratio estimator with a cluster bootstrap over base cases.
- **Controls:** 6 base cases only. Counts are reported with Wilson intervals on views and the number of control cases affected. They are descriptive only.
- **Framing.** The frozen deterministic results remain the confirmatory study. Everything in Phase 4 is exploratory and post-study. Negative results and failures are reported.
- **Claims that will not be made:** that E2 improves localization, or that B4-G eliminates false diagnoses without also reporting its missed faults.

## 7. Human audit (protocol §9.1, unchanged)

**Sample** (seed 20261101). 240 claims in total, 80 each from E0, E1 and B4, drawn from all of their pre-filter claims on the 265 views.
- **E0:** frozen E0 on the alert window, recomputed. It reproduces the frozen outputs.
- **Strata:** 40 verifier-accepted and 40 rejected per condition. For inferred claims, "accepted" means premises valid. A shortfall is filled from the other stratum and recorded.
- **Coverage and cap:** ≥ 4 per predicate group when available, and ≤ 3 claims per base case per condition.

**Blinding.** Every item uses the same rendering whatever its condition:
- the canonical claim template;
- the cited records rendered from the telemetry view itself, not from the model's table;
- ≤ 30 same-family baseline and analysis context records;
- the observed call edges;
- the window header.

Condition, prompt version, the model's wording, the verifier verdict, ground truth and case ids are concealed in `audit_key_sealed.json`.

**Annotator fields.**
- **`support_label`:** supported / partially_supported / unsupported / not_assessable.
- **`partial_reason`.**
- **`evidence_relevance`:** relevant / weakly_relevant / irrelevant. Do the cited records bear on the claim's subject and measure?
- **`notes`.**

Whether a claim concerns the true root depends on ground truth, which annotators must not see. It is therefore recorded in the sealed key from ground truth and is not asked of annotators. It is never a human label.

**Adjudication.** As in §9:
- two independent annotators;
- Cohen's κ (unweighted and weighted) and Gwet's AC1, with pre-adjudication labels kept;
- disagreements resolved by discussion, or by a third annotator if one is available;
- with a single annotator, a 20 % re-label ≥ 2 weeks later for intra-rater κ.

**No human labels exist.** The audit is reported as **pending**. Verifier verdicts are never used as labels, and no verifier accuracy is reported.

## 8. Deliverables, then STOP

**Documents:**
- `docs/phase4b_validation.md`
- `docs/phase4b_results.md`

**Results in `results/phase4b/`:**
- `llm_trials.jsonl.gz`
- `claim_metrics.json`
- `condition_comparison.json`
- `audit_blinded.csv`
- `audit_key_sealed.json`
- `runtime_summary.json`
- `plan.json`
- `repro_check.json`
- `audit_manifest.json`

Afterwards: run the regression suite, verify the freeze and hash locks, commit locally, and stop.

**Not in scope:** post-study algorithm fixes, other models, external benchmarks, and manuscript result rewriting.

## 9. Reproduce

```bash
uv run python -m phase4.eval.phase4b plan          # no model (~3 min)
colima status                                      # must be stopped
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &
caffeinate -dims uv run python -m phase4.eval.phase4b run      # resumable
caffeinate -dims uv run python -m phase4.eval.phase4b repro
pkill -f "ollama serve"
uv run python -m phase4.eval.phase4b analyze
uv run python -m phase4.eval.phase4b audit
```
