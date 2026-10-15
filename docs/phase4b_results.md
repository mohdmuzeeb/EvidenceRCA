# Phase 4B results: LLM explanations, deterministic verification and abstention on the held-out test seeds

**Scope.** This is an exploratory, post-study evaluation. The frozen deterministic study (`docs/final_results.md`) remains the confirmatory result.
- **Plan:** `docs/phase4b_plan.md`, committed at `b5bf532` before any call.
- **Model:** qwen3:4b (digest `359d7dd4…`), prompt p4a-v1, local Ollama 0.34.2. Temperature 0, seed 20261014. Cost $0.
- **Held-out caveat.** E0's deterministic test results on seeds 41/53/67 were already inspected before Phase 4B. The LLM outputs are new, but this is not an untouched holdout for the research process as a whole.
- **Verifier caveat.** "Verified" means accepted by the frozen verifier V0–V5. It is **not** independently established factual correctness. The human audit is pending.

**Units.**
- 47 fault base cases and 6 healthy-control base cases, each under 5 views:
  - C0, clean;
  - R40, 40 % record loss;
  - MT, traces missing;
  - ML, logs missing;
  - CF, conflicting decoy.
- That makes 235 fault views and 30 control views.
- The base case is the independent unit.
- Wilson intervals are on views (descriptive). The `[a, b]` intervals after case means are 95 % cluster-bootstrap intervals over base cases.

## 1. Execution

| | B4 | E1 |
|---|---|---|
| Calls (planned = made) | 265 / 265 | 247 / 247 |
| Outcome ok / schema-valid | 265 / 265 | 247 / 247 |
| Timeouts, transport failures, truncations | 0 | 0 |
| Wall time per call: mean, p95, max | 70.0 s, 88.0 s, 108.0 s | 70.5 s, 87.3 s, 103.8 s |
| Prompt / output tokens per call (mean) | 3,410 / 687 | 3,660 / 674 |
| Total LLM time | 5.15 h | 4.83 h |

**Run as a whole.**
- 10.4 h from the first call to the last, on AC power with Colima stopped, one call at a time.
- Ollama peak RSS 3.9 GB. Memory pressure was heavy, with swap around 9–10 GB of 11 GB, but nothing failed.

**Integrity checks.**
- Every request hash equalled the hash committed in the plan.
- 3 seeded re-executions were byte-identical (`repro_check.json`).
- Recomputed E0 reproduces the frozen test output hash on all 233 alerted views.

## 2. Diagnosis level (fault views, n = 235 views / 47 base cases)

| Condition | Top-1 | Case-mean top-1 [cluster 95 %] | Top-3 | Fault type | Wrong-service diagnoses | Fault views abstained |
|---|---|---|---|---|---|---|
| **B4** (LLM-only, ungated) | 74 (31.5 %) [25.9, 37.7] | 0.315 [0.213, 0.421] | 182 | 48 (20.4 %) | 160 | 1 |
| **E1** (E0 localization + LLM claims) | 148 (63.0 %) [56.6, 68.9] | 0.630 [0.545, 0.711] | 206 | 134 (57.0 %) | 87 | 0 |
| E1, frozen gated reading (= E0) | 148 | 0.630 | 204 | 134 | 85 | 2 |
| **E2** | identical to E1 by construction | | | | | |
| **B4-G** (hybrid) | 74 | 0.315 | 181 | 48 | 158 | 3 (2 gate misses) |

**Top-1 by view.**

| | C0 | R40 | MT | ML | CF |
|---|---|---|---|---|---|
| B4 | 15 | 14 | 15 | 15 | 15 |
| E1 | 38 | 20 | 15 | 37 | 38 |
| Paired exact McNemar, Holm-corrected p | 7.6e-5 | 0.42 | 1.0 | 5.9e-4 | 1.4e-4 |

**Reading the comparison:**
- **The E1 vs B4 gap.** The mean per-case difference is **+0.315 [0.179, 0.443]**. E1 is significantly better on C0, ML and CF, but **not** under record loss (R40) or missing traces (MT).
  - Under MT, E1 = B4 = 15/47, with 10 discordant cases in each direction.
  - Fault type: +0.366 [0.260, 0.472].
- **E1's advantage is E0's localization.** E1's model sees E0's ranking and B4's model does not, and E1's diagnosis is E0's by design.
  - E1's top-1 numbers are the already-reported frozen E0 test numbers. They are not a new held-out result.
  - E1's model agreed with E0's top candidate on 199 of 235 fault views (84.7 %).
- **B4's main failure is localization.**
  - Of its 160 wrong diagnoses, 103 name `inventory` and 42 name `gateway`.
  - It solved 0/15 cascade views.
  - It did slightly better than E0 on CPU pressure (17/45 vs 14/45). E0's known weakness is strong CPU faults.

## 3. Healthy controls and abstention (n = 30 views / 6 base cases)

| | False diagnoses on controls | Control base cases affected |
|---|---|---|
| **B4** | **30/30** (Wilson 95 % [88.6, 100]) | 6/6 |
| E1, with B4's window rule (median-time window when there is no alert) | 12/30 | 6/6 |
| E1, frozen gated reading (= E0) | 0/30 | 0 |
| **B4-G** | **0/30** | 0 |

**The LLM's own abstention.**
- B4 abstained on 0/30 controls and 1/235 fault views.
- E1's model recommended ABSTAIN on 0/12 controls and 1/235 fault views.
- qwen3:4b with p4a-v1 therefore effectively never abstains. B4's abstention is a single operating point with no confidence score and no risk-coverage curve.

**B4-G gate** (frozen detector, applied to B4's stored outputs):
- **Coverage:** alerted on 233/235 fault views (99.1 %) and 0/30 control views.
- **Missed faults: 2/235 (0.85 %).** Both are `41/67:cpu_pressure:payment:strong` under MT. B4 had been wrong on both, so the gate cost no correct top-1.
- **Counterfactual saving:** 32 calls (30 on controls, 2 on fault views), about 2,296 s of LLM time.
  - These calls were actually made, because B4 is ungated. No call was run for B4-G.
  - All 30 B4 false diagnoses on controls are removed.
- **Not a pure LLM-only baseline.** B4-G's abstentions come from the deterministic detector.
- **On dev, coverage is lower.** It missed 49/342 fault views (the standard-strength CPU cases, Phase 4A-v3). The 0 false diagnoses therefore come with a non-zero, data-dependent miss rate.

**E0 abstention.** The frozen S and A-margin results are unchanged (`results/final/test_results.json`):
- the S score's AURC is 0.214, worse than the margin-only ablation's AURC of 0.160;
- this is a negative result, already reported.

No new abstention score was fitted.

## 4. Claim level (frozen verifier verdicts; not human-verified)

| | B4 | E1 | E2 canonical (system-derived) | B4-G | E0 (templates, reference) |
|---|---|---|---|---|---|
| Calls (or E0 diagnoses) | 265 | 247 | 247 (no new calls) | 233 | 233 |
| Claims: observed + inferred | 1,564 + 10 | 1,452 + 3 | 390 + 0 | 1,375 + 8 | 1,082 + 233 |
| Observed verified / rejected | 454 / 1,110 | 369 / 1,083 | 152 / 238 | 408 / 967 | 1,023 / 59 |
| Verified rate | **29.0 %** [25.2, 32.8] | **25.4 %** [20.9, 30.2] | 39.0 % [30.0, 48.6] | 29.7 % | 94.5 % |
| Retained after filtering (verified / all claims) | 28.8 % | 25.4 % | 39.0 % | 29.5 % | 77.8 % |
| Verified per call | 1.71 | 1.49 | +0.62 | 1.75 | 4.39 |
| Verified claims about the true root | 78 | **158** | 38 | 77 | 659 |
| Off-target observed claims (subject neither root nor symptom service) | 478 (30.6 %) | 269 (18.5 %) | 164 | 296 | 28 |
| Rejected despite all evidence ids being valid | 1,110 | 1,082 | – | 967 | – |
| Service ownership errors (V3 fail) | 701 | 654 | 0 | 602 | 41 |
| Value errors (V4 fail) | 405 | 382 | 217 | 345 | 41 |
| Unit / schema errors (V0 fail) | 299 | 305 | 0 | 283 | 0 |
| Not above baseline (V5 fail) | 699 | 592 | 142 | 582 | 23 |
| Propagation edges: verified / total (reversed) | 49 / 608 (359) | 95 / 401 (152) | – | 42 / 538 (322) | 247 / 247 |
| Claims on controls: verified / observed | 41 / 177 | 19 / 72 | 12 / 22 | 0 | 0 |

**Citations are valid, but they do not establish support.**
- Every model claim cited only aliases that exist in the prompt, except 1 E1 claim.
- Yet 71 % (B4) and 75 % (E1) of observed claims are rejected. The failures are wrong ownership, wrong values, wrong units, or values that are not above baseline.

**Paired E1 vs B4 on claims** (53 base cases; added after the run, see validation §4):
- **Verified rate:** E1 − B4 = −0.039 [−0.094, +0.018]. There is no evidence that E1's model writes more verifiable claims.
- **Verified root claims per case:** E1 − B4 = +1.51 [+0.51, +2.66]. E1's verified claims concern the true root more often. That follows from being given E0's ranking and retrieved evidence.

**Verified rate under missing telemetry** (observed verified / observed):

| | C0 | R40 | MT | ML | CF |
|---|---|---|---|---|---|
| B4 | 82/317 | 63/311 | 152/317 | 67/302 | 90/317 |
| E1 | 82/289 | 74/302 | 56/284 | 70/285 | 87/292 |

**E2 ownership** (from E1's stored outputs; canonical claims are never counted as model-written):

| Disposition of E1's 1,452 observed claims | Count |
|---|---|
| Consistent | 492 |
| Transformed (separate canonical candidate) | 390 (26.9 %) |
| Rejected (no repair possible) | 560 (38.6 %) |
| Not applicable | 13 (with the inferred claims) |

- **Transformed claims:** 352 had subject-ownership errors and 190 had unit changes. Values are never rescaled.
- **Rejected claims:**
  - 372 cite an inadmissible record family;
  - 187 cite records implying different subjects;
  - 1 cites a missing record.
- **Canonical claims:** 152/390 verify (39.0 %). 217 fail V4 because values are never edited, and 21 fail V5.
  - Only 38 verified canonical claims concern the true root; 80 are off-target.
- **Verified per call:** 1.49 (original only), 2.11 (original + canonical, counted separately).
- **Localization:** E2 does not change localization.

## 5. What the results support, and what they do not

**Supported, within verifier-acceptance limits:**
- Locally run qwen3:4b, used as an end-to-end RCA agent (B4), localizes poorly: 31.5 % top-1. It never abstains, and diagnosed every healthy control.
- With frozen EvidenceRCA ranking and retrieval (E1), localization is E0's (63.0 %). That is significantly better than B4 on clean, log-less and conflicting views, but not under record loss or missing traces.
- In both conditions, most LLM-written claims fail deterministic verification (71–75 %), even though nearly all citations are valid ids. Deterministic verification filters these claims. E0's template claims verify at 94.5 %.
- A detector gate removes all control false diagnoses on the test seeds, at a measured cost of 2/235 missed fault views (14.3 % on dev).

**Not supported:**
- That the LLM improves localization (E1's localization is E0's).
- That E1's model writes more faithful claims than B4's (no significant difference; point estimate slightly lower).
- That E2 improves localization.
- That B4-G eliminates false diagnoses without missed faults.
- Any verifier accuracy, or hallucination rate in the human sense. Both require the pending audit.

## 6. Remaining work for the IEEE paper

1. **Human audit.**
   - Two independent annotators label `results/phase4b/audit_blinded.csv` (240 items) using `results/phase4b/audit_instructions.md`.
   - The study lead must first write the 10 dev-only worked examples.
   - Then adjudicate, unseal `audit_key_sealed.json`, and compute verifier precision/recall and per-condition unsupported rates.
   - Until then the audit is **pending**, and "verified" must be described as verifier acceptance only.
2. **Framing in the manuscript:**
   - LLM conditions are post-study and exploratory;
   - the held-out seeds were previously inspected for the deterministic methods;
   - E1's information advantage (E0's ranking) must be stated;
   - B4-G is hybrid.
3. **Limitations to state:**
   - one small local model (4B, quantized) and one prompt;
   - 47 + 6 base cases from one testbed;
   - only 6 control cases;
   - R40/MT conclusions are underpowered;
   - verifier rules and E2 ownership share association logic;
   - the rendered-precision artifact in V4 (9 B4, 2 E1 cases).
4. **Not done** and requiring separate approval:
   - post-study algorithm fixes;
   - other or larger models;
   - external benchmarks (e.g., RCAEval);
   - an E1-specific abstention score on new seeds (protocol §7);
   - manuscript rewriting.
