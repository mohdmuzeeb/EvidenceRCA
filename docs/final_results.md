# Final Results: Calibration and Held-out Test (deterministic methods)

| | |
|---|---|
| Frozen protocol | [`docs/frozen/frozen_protocol.md`](frozen/frozen_protocol.md); manifest of 45 hashed files; freeze commit `912085c`, verified before every capture and evaluation stage |
| Calibration | Seeds 23/37 only → [`results/final/calibration.json`](../results/final/calibration.json) (sha256 `e062942d…`), committed in `fb6a9fd` **before** any test label was read |
| Test | Seeds 41/53/67 → [`results/final/test_results.json`](../results/final/test_results.json) (records the calibration hash); raw per-trial predictions and abstention decisions in `results/final/test_trials.jsonl.gz` |
| Sensitivity | [`test_results_sensitivity_adjudicated.json`](../results/final/test_results_sensitivity_adjudicated.json) (§2) |
| Scope | Deterministic methods only. No LLM, no model download, no paid API. Algorithms, thresholds and rules were unchanged after viewing test results. |

## 1. Capture

| Split | Scenarios | Valid | Captures | Invalid → recaptured | Reasons |
|---|---|---|---|---|---|
| calibration (23, 37) | 36 | **36** | 40 | 4 | 2 × recovery tail after an inventory crash or pool release (p95 in the last 30 s above the frozen limit); 2 × one container's docker_stats series never discovered by the collector |
| test (41, 53, 67) | 54 | **53** | 56 | 3 | 1 × docker_stats discovery miss (recaptured); **2 × the same scenario, 41:network_timeout:inventory** (§2) |

- **Integrity:** 369 raw files re-hashed equal to their manifests.
- **Aborted:** 1 case, started when AC power was lost at 20:10. Capture was paused at the case boundary and the partial case quarantined. Every valid case ran on AC.
- **Capture continuity:** no sleep or stall in any capture (max monitor gap ≤ 6 s).
- **Host during capture:** the watcher logged memory-status free 32–41 % and 24–29 GB of free disk.
- **Test set:** 47 single-fault + 6 controls:

  | Fault type | Cases |
  |---|---|
  | processing_latency | 12 |
  | service_crash | 9 |
  | cpu_pressure (strong) | 9 |
  | network_timeout | 8 |
  | db_pool_exhaustion | 6 |
  | cascading_failure | 3 |

  Dual-fault scenarios are not captured (protocol deviation, documented).

## 2. The excluded test scenario (reported, not hidden)
- **What happened:** 41:network_timeout:inventory failed the frozen validity rule on both attempts. One monitor sample's read interval straddles the injection instant (−0.14 s to +0.02 s); it is time-stamped at its midpoint, just before the recorded start, so the qdisc signature counts as "present before injection".
- **Why it recurs:** the seed fixes both the monitor phase and the onset, so every recapture reproduces it.
- **The fault really occurred:** the independent TCP probe shows it in 100 % of injection samples and 0 % before and after.
- **Handling:** the primary analysis follows the frozen rule and excludes the scenario (n = 47). The sensitivity run includes the second attempt as an adjudicated case (n = 48):

  | | Primary (n = 47) | Sensitivity (n = 48) |
  |---|---|---|
  | E0 C0 top-1 | 38/47 | 39/48 |
  | E0 S AURC | 0.214 | 0.214 |
  | E0 A-margin AURC | 0.160 | 0.158 |

  No conclusion changes.
- **Root cause:** a validator time-attribution bug. A sample containing the injection instant should be treated as ambiguous. It is recorded for a post-study fix and was not changed under the freeze.

## 3. Detection
The SLO alert (alert-anchored, the primary analysis):

| View | Fault trials detected | Control trials alerted |
|---|---|---|
| C0 | **47/47** (Wilson 95 % CI 0.92–1.00) | **0/6** |
| R20, R40, R60 (each) | 141/141 | 0/18 |
| **MT (no traces)** | **45/47** (0.86–0.99) | 0/6 |
| ML, MM, CS, RL, J1, J2, CF | 47/47 | 0/6 |

The strong CPU variant made every CPU case detectable (cf. dev: standard CPU 0/3).

## 4. Localization: top-1 over all fault trials (detection failures count as wrong), 95 % CI

R* rows are the mean over 3 perturbation seeds per base case, with a cluster-bootstrap CI; the other rows use Wilson.

| View | B1 | B2 | B3 | **E0** | E0-A-graph | E0 top-3 | E0 fault type (of diagnosed) |
|---|---|---|---|---|---|---|---|
| C0 | 0.26 [0.15, 0.40] | 0.53 [0.39, 0.67] | 0.09 [0.03, 0.20] | **0.81 [0.67, 0.90]** | 0.45 [0.31, 0.59] | 0.94 | 35/47 |
| R20 | 0.28 | 0.54 | 0.07 | **0.69 [0.60, 0.77]** | 0.47 | 0.85 | 77/141 |
| R40 | 0.34 | **0.53 [0.39, 0.67]** | 0.06 | 0.47 [0.38, 0.56] | 0.44 | 0.79 | 35/141 |
| R60 | 0.31 | 0.54 | 0.12 | **0.62 [0.52, 0.71]** | 0.41 | 0.82 | 58/141 |
| MT (no traces) | 0.21 | 0.00 | 0.00 | **0.32 [0.20, 0.46]** | 0.32 | 0.74 | 19/45 |
| ML (no logs) | 0.21 | 0.51 | 0.09 | **0.79** | 0.53 | 0.94 | 32/47 |
| MM (no metrics) | 0.26 | 0.06 | 0.09 | **0.81** | 0.47 | 0.94 | 26/47 |
| CS (root spans removed) | 0.04 | 0.68 | 0.02 | **0.94 [0.83, 0.98]** | 0.26 | 0.94 | 35/47 |
| RL (root logs removed) | 0.26 | 0.53 | 0.09 | **0.81** | 0.43 | 0.94 | 35/47 |
| J1 / J2 | 0.26 / 0.28 | 0.55 / 0.55 | 0.09 / 0.06 | **0.81 / 0.79** | 0.43 / 0.49 | 0.94 | 35 / 32 |
| CF (decoy) | 0.26 | 0.53 | 0.09 | **0.81** | 0.45 | 0.94 | 35/47 |
| Oracle-anchored C0 (sensitivity) | 0.32 | 0.55 | 0.06 | **0.77** | 0.49 | — | — |

**Per fault type (C0 top-1):**

| Fault type | B1 | B2 | B3 | E0 | E0-A-graph |
|---|---|---|---|---|---|
| processing_latency | 5/12 | 9/12 | 3/12 | **12/12** | 9/12 |
| service_crash | 0/9 | 0/9 | 0/9 | **9/9** | 1/9 |
| network_timeout | 0/8 | 7/8 | 1/8 | **8/8** | 0/8 |
| db_pool_exhaustion | 3/6 | 6/6 | 0/6 | **6/6** | 6/6 |
| cpu_pressure (strong) | **4/9** | 3/9 | 0/9 | 3/9 | **5/9** |
| cascading_failure | 0/3 | 0/3 | 0/3 | **0/3 (top-3 0/3)** | 0/3 |

**Paired comparisons at C0** (E0 minus competitor, top-1; cluster-bootstrap CI; exact McNemar; Holm-adjusted over 4 comparisons; n = 47 base cases):

| E0 vs | Difference | 95 % CI | Discordant pairs (E0 only / competitor only) | Holm p |
|---|---|---|---|---|
| B1 | +0.55 | [0.36, 0.72] | 30 / 4 | 2e−5 |
| B2 | +0.28 | [0.15, 0.40] | 13 / 0 | 5e−4 |
| B3 | +0.72 | [0.60, 0.85] | 34 / 0 | < 1e−6 |
| E0-A-graph | +0.36 | [0.17, 0.55] | 22 / 5 | 0.0015 |

Under other views (bootstrap CI, no multiplicity correction across views):
- **R40:** E0 vs B2 = **−0.06 [−0.21, 0.08]**; heavy random loss erases E0's advantage over B2.
- **MT:** E0 vs B1 = +0.11 [−0.04, 0.26], not distinguishable.
- **CS:** E0 vs B2 = +0.26 [0.13, 0.38].
- **MM:** E0 vs B2 = +0.74 [0.57, 0.89].

## 5. Abstention (alert-anchored, diagnosed fault trials pooled over all views; τ from calibration only)

| Score | AURC [95 % CI] | op1: τ → coverage / selective risk (test) | op2: τ → coverage / selective risk (test) |
|---|---|---|---|
| **E0 S** (pre-registered, equal weights) | 0.214 [0.146, 0.296] | 0.568 → 0.72 / 0.28 [0.25, 0.32] | 0.958 → 0.002 / 0.0 (degenerate) |
| **E0 A-margin** (mandated competitor) | **0.160 [0.096, 0.252]** | 0.103 → 0.71 / **0.22** [0.19, 0.25] | 1.0 → **0.46 / 0.095** [0.07, 0.13] |
| E0 A-ver (S without verified coverage) | 0.204 [0.142, 0.277] | 0.445 → 0.74 / 0.28 | 0.889 → 0.002 / 0.0 |
| E0 A-abst (never abstain) | — | coverage 1.0 / risk 0.32 | — |
| E0-A-graph S | 0.658 | 0.333 → 1.00 / 0.57 | not achievable |
| B1 margin | 0.760 | 0.280 → 0.80 / 0.74 | 1.0 → 0.0 / — |
| B2 margin | 0.174 [0.086, 0.294] | 0.150 → 0.87 / 0.25 | 0.293 → 0.28 / **0.040** |
| B3 margin | 0.900 | 0.0 → 1.00 / 0.92 | not achievable |

- **Controls:** none diagnosed at any operating point (0/108 control trials).
- **Negative finding, as on dev:**
  - the pre-registered 4-signal S is **worse** than margin alone (AURC 0.214 vs 0.160; the CIs overlap);
  - S's op2 (risk ≤ 0.1) is reachable only at ~0.2 % coverage;
  - removing verified-claim coverage (A-ver) slightly *improves* AURC (0.204), so the verifier signal did not help ranking confidence here.
- **Selective prediction works best with E0's margin (A-margin):** at τ = 1.0 it keeps 46 % of diagnoses with 9.5 % risk, against 32 % risk without abstention.
- **S and margins are heuristic ordering scores, not probabilities.**

## 6. Verifier behaviour on E0's claims (alert-anchored)
- **Observed claims verified:**

  | View | Verified / observed | Rate |
  |---|---|---|
  | C0 | 219/234 | 93.6 % |
  | R40 | 761/824 | 92.4 % |
  | MM | 157/176 | 89.2 % |
  | MT | 118/118 | 100 % |

  Verified claims per DIAGNOSE: 2.6 (MT) to 5.4 (R40).
- **Propagation edges** passed V6 in every view (for example 65/65 at C0). No DIAGNOSE was flagged.
- **Rejections are almost all `onset_before`** (265 of 290):
  - V3/V4: E0 cites a caller's span as the object's first anomaly (the known E0 limitation after the approved correction);
  - V5: the independent onset order disagrees;
  - V2: the cited record lies outside the window.

  The rest are call_failed / error_rate claims failing V5 (25). All are rejected and reported, never rewritten.
- These are the verifier's own judgements; its precision on real claims still needs the human audit (≥ 200 claims; not performed).

## 7. Runtime
| Method | Rank p50 / p95 | Verify p50 / p95 |
|---|---|---|
| B1 | 14 / 27 ms | — |
| B2 | 26 / 48 ms | — |
| B3 | 38 / 66 ms | — |
| E0 | 56 / 93 ms | 128 / 290 ms |

- Test evaluation (9540 trials): 925 s.
- Calibration (6480 trials): 569 s.
- Capture: ~5.7 min per case. 97 captures took about 9.5 h of capture time, plus one AC-loss pause.

## 8. Limitations
1. **One environment:** a 5-service synthetic testbed on one machine. n = 47 test faults, with 3 per postgres-rooted fault. CIs are wide; per-fault results are descriptive.
2. **E0 was developed on dev seed 11.** Test results are held out, but every design decision was made by one developer on 21 dev cases.
3. **Unsolved by every method:**
   - cascading failure (0/3 top-1 for all; E0 not even top-3);
   - strong CPU starvation (E0 3/9; it often blames the DB/pool symptoms).
4. **Robustness gaps:**
   - without traces, E0 drops to 0.32;
   - under 40 % random record loss, E0 loses its advantage over B2.
5. **Abstention:** the pre-registered composite S underperforms the simple margin, and the verifier-coverage signal adds nothing to selective risk on this data.
6. **Verifier:**
   - `onset_before` rejections dominate, because E0's citation choice conflicts with the corrected association rule;
   - accuracy on real claims is not established (no human audit).
7. **B2/B3 are "inspired by" their papers;** they are not validated against the authors' code.
8. **Validity tooling:**
   - one scenario was lost to a validator time-attribution artifact (§2);
   - docker_stats discovery misses occurred in 3/97 captures (caught and recaptured).
9. **Protocol deviations:** dual-fault scenarios were not captured. The verifier correction (approved) changed `onset_before` verdicts relative to Phase 3.

## 9. Proposed next steps (each needs your approval)
1. **Phase 4: LLM integration (E1/B4)**, with a local model. qwen3:4b must be downloaded first (~2.5 GB, needs approval). Run the timed 5-call benchmark first. Docker must not run alongside.
2. **The human audit** of ≥ 200 verifier judgements (blinded sheet tool ready).
3. **Post-study fixes,** reported separately and never retro-applied to these results:
   - the validator boundary artifact;
   - the E0 onset-citation choice;
   - the cascade attribution.
4. If time permits: more seeds, to narrow the CIs, and the RCAEval external validation (per-case download).
