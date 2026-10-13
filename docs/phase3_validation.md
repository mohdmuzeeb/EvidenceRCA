# Phase 3: Deterministic Baselines, EvidenceRCA (no LLM), Evidence Verifier and Abstention

| | |
|---|---|
| **Outcome** | Implementation complete; all 102 tests pass; Phase 1 and Phase 2 regressions pass. Development results only (seed 11). |
| Results | [`dev_results.json`](../results/phase3/dev_results.json) (3820 trials), [`verifier_tests.json`](../results/phase3/verifier_tests.json), [`abstention_dev_results.json`](../results/phase3/abstention_dev_results.json), [`cpu_variant_analysis.json`](../results/phase3/cpu_variant_analysis.json), [`leakage_regression.json`](../results/phase3/leakage_regression.json), [`phase1_regression/`](../results/phase3/phase1_regression/) |
| Specifications | [`baseline_implementation.md`](baseline_implementation.md), [`evidence_verifier_spec.md`](evidence_verifier_spec.md), [`experiment_protocol.md`](experiment_protocol.md) §12 deviations and §13 freeze candidate |

**Read this first:**
- Every number here is from **dev seed 11**: 21 base cases, of which 19 have a fault, plus 2 controls.
- EvidenceRCA (E0) was **designed and debugged on these same cases** (§7), so its dev accuracy is optimistic by construction.
- Sample sizes are tiny: one cascading-failure case, 2–4 cases per other fault type.
- **No statistical significance is claimed.** Differences here say nothing about the test seeds.
- No calibration or test case exists or was opened.
- No LLM was used. Ollama was not installed or started, and no paid API was called.

## 1. Pre-flight
- Git was clean at `1925e33`, and 42/42 tests passed.
- All 63 raw files re-hashed equal to their manifests, and every raw file was read-only.
- The ground-truth index contained only seed 11.
- The protocol and fault catalog were reviewed. Raw captures and labels were never modified: analysis-code fixes only recompute verdicts from recorded files (§7).

## 2. Data captured in Phase 3 (dev seed 11 only)

| Set | Cases | Valid | Notes |
|---|---|---|---|
| Dev matrix: 16 single-fault + 2 controls (protocol timeline) | 14 new + 4 reused from pilot run A | **18/18** scenarios | 3 captures were invalid and recaptured: one because of host contention from my own analysis running concurrently (§7.6), two because of lid-close sleeps (caught by the continuity check, quarantined in `data/aborted/`) |
| Strong CPU-pressure variant (order, payment, inventory) | 3 | **3/3** | Same workload schedule and onset as the standard variant (paired) |
| Pilot run B | 4 | 4/4 | Used only as replicates |

## 3. CPU-pressure investigation

| Variant / target | Target CPU (cores, median in injection) | Throttled-period ratio | All containers (cores) | Gateway p95 baseline → injection | Agent alert | E0 top-1 / type |
|---|---|---|---|---|---|---|
| standard / inventory | 1.00 | 1.00 | 1.25 | 37 → 72 ms | no | — |
| standard / order | 1.00 | 1.00 | 1.28 | 61 → 90 ms | no | — |
| standard / payment | 1.00 | 1.00 | 1.29 | 42 → 65 ms | no | — |
| strong / order | 0.20 | 1.00 | 0.56 | 41 → 4141 ms | yes | order ✓ / db_pool_exhaustion ✗ |
| strong / payment | 0.20 | 1.00 | 0.57 | 36 → 268 ms | yes | payment ✓ / processing_latency ✗ |
| strong / inventory | 0.20 | 1.00 | 0.53 | 38 → 4160 ms | yes | postgres ✗ / cascading_failure |

**Why the standard variant is weak:**
- The burners do saturate the container's 1.0-CPU quota (throttled ratio 1.0).
- But a service at 10 req/s needs only ~0.05 cores, and CFS fair sharing among 3 runnable processes still leaves it ~0.33 cores. Latency rises only by scheduling delay, +23–35 ms at p95.
- It is a real fault, independently validated, that this SLO-anchored workload barely notices.

**The strong variant:**
- It lowers the target container's own quota to 0.2 CPU.
- It is container-scoped and reversible: the quota was restored exactly in 3/3 cases.
- It **reduces** host CPU, from ~1.27 to ~0.55 cores across all containers.
- macOS reported no thermal, performance or CPU-power warnings (`pmset -g therm`).
- Starved services queue requests until pool and DB timeouts. That makes the fault observable, but it **also makes it look like DB/pool trouble**: E0 types 2/3 wrongly, and for inventory it blames postgres.

**Both variants are kept.** The standard cases count as *detection failures* (no alert). Whether the calibration/test matrix uses `strong` is **your decision** (protocol §13, item 1).

## 4. Implementation completeness

| Requirement | Status | Where |
|---|---|---|
| B1 robust statistical ranking | done | `methods/baselines.py` |
| B2 MicroRCA-inspired: BIRCH 0.045, Alg. 1 weights, Eq. 3 score, Eq. 4 PPV on reversed edges | done; **not** validated against author code | same, doc §B2 |
| B3 TraceRCA-inspired: severity, δ_fs relative selection, δ_ad, trace-level sets, JI top-100, in-set score | done; **not** validated against author code | same, doc §B3 |
| Tests: cyclic and disconnected graphs, missing modalities, empty evidence | done (15 tests) | `tests/test_baselines.py` |
| EvidenceRCA E0: observed telemetry, temporal onset, dependency graph, evidence retrieval, typed claims; A-graph ablation | done | `methods/evidencerca_e0.py`, `signals.py`, `core.py` |
| Never reads ground truth during inference | enforced: the agent side imports no harness/evaluation code (static test); reading goes only through `rca_input` | `harness/leakage.py`, tests |
| Verifier V0–V6, closed vocabulary, JSON output | done; independent implementation (test-enforced) | `verifier.py`, `evidence_verifier_spec.md` |
| Verifier tests: false citations, altered values, wrong services, missing records, fabricated edges, contradictory evidence | done (16 unit tests + mutation suite) | `tests/test_verifier.py`, `evaluation/verifier_tests.py` |
| Abstention: sufficiency, modality agreement, margin, verified-claim coverage; A-margin ablation | done; τ configurable and **not** calibrated | `abstention.py` |
| Deterministic clean and perturbed views (18 per case) on matched evidence; leakage regression tests | done (10 tests) | `evaluation/views.py`, `tests/test_views_leakage.py` |
| Human audit ≥ 200 claims | **protocol and blinded-sheet tool only; not performed** | spec §5.2, `evaluation/audit_sample.py` |

## 5. Development results (alert-anchored windows; denominators shown)

The 19 fault cases × each view; R20/R40/R60 each pool 3 perturbation seeds. Top-1 and top-3 counts are over **triggered** fault trials. Detection failures are shown separately.

| Condition | Triggered / fault trials | B1 top-1 | B2 top-1 | B3 top-1 | **E0 top-1** | E0-A-graph top-1 | E0 top-3 | E0 fault type | Controls alerted |
|---|---|---|---|---|---|---|---|---|---|
| C0 | 16/19 | 4 | 9 | 2 | **14** | 8 | 15 | 11 | 0/2 |
| R20 | 49/57 | 11 | 27 | 5 | **30** | 24 | 44 | 26 | 0/6 |
| R40 | 50/57 | 13 | **29** | 6 | 25 | 17 | 41 | 15 | 0/6 |
| R60 | 49/57 | 12 | 27 | 8 | **31** | 20 | 44 | 23 | 0/6 |
| MT (no traces) | 16/19 | **4** | 0 | 0 | **4** | 4 | 13 | 6 | 0/2 |
| ML (no logs) | 16/19 | 6 | 9 | 2 | **13** | 8 | 15 | 10 | 0/2 |
| MM (no metrics) | 16/19 | 5 | 1 | 2 | **13** | 9 | 14 | 9 | 0/2 |
| CS (root spans removed) | 16/19 | 0 | 11 | 2 | **15** | 3 | 15 | 11 | 0/2 |
| RL (root logs removed) | 16/19 | 4 | 9 | 2 | **14** | 8 | 15 | 11 | 0/2 |
| J1 (±0.5 s) | 16/19 | 6 | 9 | 2 | **13** | 7 | 15 | 12 | 0/2 |
| J2 (±2 s) | 17/19 | 6 | 10 | 2 | **14** | 7 | 16 | 11 | 0/2 |
| CF (decoy) | 16/19 | 4 | 9 | 2 | **14** | 8 | 15 | 11 | 0/2 |

**C0 by fault type (top-1 / cases):**

| Fault type | B1 | B2 | B3 | E0 | E0-A-graph |
|---|---|---|---|---|---|
| processing latency | 2/4 | 3/4 | 1/4 | 4/4 | 3/4 |
| crash | 0/3 | 0/3 | 1/3 | 3/3 | 1/3 |
| network timeout | 0/3 | 3/3 | 0/3 | 3/3 | 0/3 |
| pool exhaustion | 2/2 | 2/2 | 0/2 | 2/2 | 2/2 |
| cascading failure | 0/1 | 0/1 | 0/1 | **0/1** | 0/1 |
| cpu standard | not triggered (0/3) | | | | |
| cpu strong | 0/3 | 1/3 | 0/3 | 2/3 | 2/3 |

**Observations (dev; descriptive only):**
- **Detection failures:** the 3 standard-CPU cases never alert, in any view. Of 19 fault cases, 16 alert on C0.
- **Controls:**
  - no control alerted in any alert-anchored view (0/36 trials), so no method diagnosed a control;
  - with forced oracle windows, B1 diagnosed both controls, while B2, B3 and E0 did not.
- **The graph constraint matters:** E0 vs its A-graph ablation is 14 vs 8 on C0, and 15 vs 3 on CS.
- **E0 failure modes seen on dev:**
  - the cascade (postgres lock) is attributed to inventory, which shows pool saturation;
  - strong CPU on inventory is attributed to postgres;
  - fault-type inference degrades under record loss (R40: 15/50);
  - **without traces (MT), localization drops to 4/16.**
- **B2 is strongest under heavy random loss** (R40: 29 vs E0 25).
- **Sensitivity (oracle windows, C0, all 19 fault cases):** B1 8, B2 9, B3 2, E0 15, E0-A-graph 9.
- **Replicates (pilot run B vs A, C0, 5 methods × 2 anchors × 4 scenarios):** 39/40 identical outputs. The one difference is B3 on a control under an oracle window.

## 6. Verifier and abstention

### Verifier, by construction (`verifier_tests.json`; ground truth known without the verifier)

| Mutation of real E0 claims | n | Rejected | Rejected by the targeted check |
|---|---|---|---|
| false citation → V1 | 84 | 84 | 84 |
| cited record removed → V1 | 84 | 84 | 84 |
| window outside data → V2 | 84 | 84 | 84 |
| wrong subject service → V3 | 84 | 84 | 80 (4 by V4/V5) |
| value ×3 + 1 → V4 | 84 | 84 | 84 |
| unit swapped → V4 | 59 | **58** | 54 |
| contradictory evidence (normal records cited) → V5 | 61 | 61 | 61 |
| fabricated propagation edge → V6 | 43 | 43 | 43 |
| "no telemetry" from one missing record → V5 | 64 | 64 | — |

- Real propagation edges: 21/21 pass V6.
- Inferred causal claims: 16, all with valid premises, and **0 counted as verified facts**.
- **The one accepted unit swap** is the same `onset_before` claim that the verifier wrongly rejects natively. Its 0.721 s → 0.721 ms change happens to match the verifier's mis-attributed recomputation of 0. Root cause: a spec ambiguity in V4 association for relational claims (protocol §13, item 2). **Not patched**, to avoid fitting the verifier to the pipeline's own claims.

### Verification of E0's native claims (alert-anchored, all views)
- 91–100 % of observed claims verify per condition (C0: 81/84; R40: 255/280; MT: 41/41).
- All rejections are reported, never rewritten. They are mostly `onset_before` (V4 27, V5 21, V2 17), plus call_failed / error_rate / timeout claims failing V5 (13).
- No DIAGNOSE was flagged (each had at least one verified claim about the suspected service).
- These rates are *the verifier's own judgment*. Its accuracy on real claims needs the human audit (§4), which is not yet done.

### Abstention (`abstention_dev_results.json`; descriptive, τ not calibrated)
- AURC over all diagnosed fault trials (lower is better):

  | Score | AURC | n |
  |---|---|---|
  | E0, S (4 signals) | 0.231 | 293 |
  | **E0, A-margin** | **0.142** | 293 |
  | E0-A-graph, S | 0.688 | 293 |
  | B1 margin | 0.754 | 293 |
  | B2 margin | 0.139 | 215 |
  | B3 margin | 0.953 | 277 |

- **On dev, the mandated simple competitor (A-margin) beats the composite S.** This is reported as found; S was not re-weighted (protocol §13, item 3).
- Example operating points for E0's S, which are illustrative and not chosen:
  - τ = 0.5: coverage 0.88, selective risk 0.32;
  - τ = 0.7: coverage 0.42, selective risk 0.16.
- S and A-margin are heuristic ordering scores, **not probabilities**.

## 7. Problems found and fixed during development (all on dev data)

1. **DB pool waits were misattributed to postgres:** SQLAlchemy's `connect` spans include the pool wait. Fixed in normalization, with a new `pool_wait` signal.
2. **App metrics were not aggregated across route/status series:** the detector's metric fallback read a random series. Fixed with request-weighted aggregation per export.
3. **The view builder matched `RL` as random loss.** Caught by a test.
4. **Verifier V4 for `onset_before` conflated V4 with V5.** Changed so that V4 checks the cited records and V5 checks onsets independently. An association ambiguity remains (§6).
5. **B2/B3 were re-aligned to the papers' text** after reading the PDFs: the graph window, the host weight factor, Eq. 4, relative feature selection, trace-level sets and in-set score. No parameters changed.
6. **My analysis ran concurrently with a capture** and slowed the testbed, invalidating one case (recovery p95 299 ms). It was recaptured, and analysis now never overlaps a capture.
7. **Two lid-close sleeps on battery** invalidated captures. They were detected by `capture_continuity`, quarantined and recaptured.
8. **Evaluation was too slow (2.3 s/trial):** lookups are now indexed, at 0.06 s rank + 0.13 s verify for E0 (p50).
9. **The replicate-consistency check compared different scenarios.** Fixed to match on the scenario key.

E0's thresholds, floors and rules were set while looking at dev results. That is what the dev split is for, but it makes the dev numbers optimistic.

## 8. Runtime and resources (evaluation process, Docker stopped)

| Method | Rank p50 / p95 | Verify p50 / p95 |
|---|---|---|
| B1 | 14 / 28 ms | — |
| B2 | 27 / 50 ms | — |
| B3 | 37 / 68 ms | — |
| E0 | 56 / 99 ms | 128 / 303 ms |
| E0-A-graph | 38 / 64 ms | 46 / 141 ms |

- Normalization: 1.4 s per case (p50).
- The full dev evaluation (3820 trials) took 346 s, with peak RSS 703 MB.
- Capture: ~5.7 min per case; `data/` holds 1.7 GB.

## 9. Tests and regressions
- **`pytest`:** 102 passed:
  - Phase 1 validator self-test (7);
  - Phase 2 leakage (54, including per-captured-case parameterizations);
  - Phase 3 baselines (15), verifier (16) and views/leakage (10).
- **Phase 1 regression** (`results/phase3/phase1_regression/`): smoke 7/7, telemetry 22/22.
- **Phase 2 leakage regression** (`results/phase3/leakage_regression.json`): all checks pass over 27 experiment, 12 mechanism and 1 load bundles.

## 10. Remaining limitations
1. Tiny dev n, and E0 was tuned on it. No significance and no generalization claim; the test seeds are needed.
2. **B2 and B3 are not validated** against the authors' implementations.
3. The **human audit** of verifier accuracy is not done, and is required before any claim about verifier precision.
4. Verifier V4 has a relational-claim association ambiguity (§13.2), pending your decision.
5. The composite abstention score loses to A-margin on dev (§13.3).
6. Root-cause **typing** is weak for CPU starvation and DB-lock cascades; postgres-rooted faults are under-represented (1 dev case).
7. **Without traces**, E0 localization collapses (4/16).
8. Captures need AC power and the lid open (two sleeps this phase); about 13 h for the calibration/test matrix.
9. qwen3:4b is still not installed (Phase 4).

## 11. Not done (by instruction)
- No LLM integration (Phase 4).
- No qwen3:4b download, and no paid API.
- No calibration/test capture, and no full six-seed matrix.
- τ not calibrated, and no test label accessed.
