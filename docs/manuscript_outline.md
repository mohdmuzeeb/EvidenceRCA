# Manuscript outline (ICICYTA 2026, IEEE conference format)

**Status:** outline only. The paper is not drafted or submitted. The human audit is **PENDING**, so every place that needs its results is marked `[AUDIT PENDING]`.

**Inputs.** Every number below comes from recorded results through `paper/build_artifacts.py`:
- tables in `paper/tables/*.tex` (preview: `paper/tables/README.md`);
- figures in `paper/figures/*.pdf` (preview: `paper/figures/README.md`);
- number macros in `paper/numbers.tex`.

In the manuscript, use the macros and the generated tables instead of typing numbers.

**Two tiers of evidence, which must stay visibly separate throughout the paper:**

| Tier | What | Source | How to word it |
|---|---|---|---|
| **Confirmatory** | Deterministic E0 and baselines B1–B3 and E0-A-graph. Protocol frozen at `912085c`, thresholds fixed on calibration seeds 23/37, evaluated once on held-out seeds 41/53/67 | `results/final/`, `docs/final_results.md` | "held-out", "pre-registered" |
| **Exploratory** | LLM conditions B4, E1, E2 and B4-G (Phase 4A–4B). Designed after the deterministic test results were seen; the Phase 4B matrix was pre-registered (`b5bf532`) but ran on the same, already-inspected test seeds | `results/phase4b/`, `docs/phase4b_results.md` | "exploratory", "post-study"; never "held-out confirmation" |

Every exploratory table caption already carries this label, and the text must repeat it where results are introduced.

---

## Proposed title (choose one)

1. *Dependency-Constrained Root-Cause Localization with Auditable Evidence Verification: What a Small LLM Adds, and What It Does Not*
2. *EvidenceRCA: Dependency-Constrained Localization and Deterministic Claim Verification for Microservice Root-Cause Analysis under Incomplete Telemetry*

The working title in `docs/research_plan.md` promised an "uncertainty-aware ... agent". The pre-registered abstention score lost to its own margin component, and the LLM did not abstain on healthy controls. The title should therefore not lead with uncertainty-awareness or agency.

## Abstract (≈ 200 words; structure only)

1. **Problem.** Root-cause analysis for microservices under incomplete telemetry. LLM explanations are attractive, but they are unverifiable.
2. **Approach.** E0 combines dependency-constrained localization (ranking constrained by observed call edges and onset order) with a deterministic verifier. The verifier checks every factual claim against cited telemetry records (V0–V6). E0 abstains on margin and evidence scores.
3. **Confirmatory result.** On a frozen protocol with 47 held-out fault cases and 6 controls, E0 localizes 0.81 [0.67, 0.90] top-1 on clean telemetry, against 0.53 for the strongest baseline (paired +0.28 [0.15, 0.40]). It diagnoses no healthy control. Its advantage disappears under 40 % random record loss and without traces.
4. **Exploratory result.** A local 4-billion-parameter LLM (qwen3:4b), used alone, localizes 0.315 and diagnoses all 30 healthy-control views. With E0's ranking, LLM-written claims are accepted by the verifier at only 25–29 %. The failures are mostly ownership, unit and value errors, despite valid record citations.
5. **Takeaway.** Keep the LLM out of localization and gating, and verify every model-written claim. The verifier's own accuracy is to be established by a blinded human audit `[AUDIT PENDING]`.

## I. Introduction (≈ 0.75 page)
- **Motivation:** on-call diagnosis with partial telemetry, and the growing use of LLMs to write incident explanations.
- **Gap:**
  - graph and trace RCA methods (MicroRCA, TraceRCA and others) rank services but do not produce checkable evidence;
  - LLM-RCA systems that "verify" claims (CausaLLM-Graph's VerifyAndFilter; EviRCA's evidence cards) report no verifier accuracy;
  - few report abstention or controls.

  Sources: `docs/literature_matrix.md` §1 and §3. Before submission, read the papers there marked "abstract only / snippet only": EviRCA, GALA, PACE-LM, SpecRCA, "Beyond Fault Localization".
- **Contributions** (state exactly these; do not inflate):
  1. A dependency-constrained localizer and a deterministic, rule-based claim verifier (V0–V6), evaluated under a frozen, pre-registered protocol with calibration/test separation and healthy controls.
  2. A robustness study over 12 telemetry views (record loss, missing modalities, root-evidence removal, clock jitter, decoys), including negative results.
  3. An exploratory, pre-registered evaluation of a local LLM, alone and on top of E0. It shows where model-written explanations fail verification, and that the LLM does not abstain.
  4. A released blinded audit package (240 claims, sealed key) and its workflow. The audit's results are reported once complete `[AUDIT PENDING]`.
- **Do not claim:** state-of-the-art accuracy; verifier precision or recall; that the LLM improves localization; that E2 improves localization; that B4-G "eliminates" false diagnoses.

## II. Related work (≈ 0.5 page)
- **Graph, trace and causal RCA:** MicroRCA (B2-inspired), TraceRCA (B3-inspired), MicroCause, DiagFusion, CauseLens.
- **LLM-based RCA and verification:** CausaLLM-Graph, EviRCA, GALA, SpecRCA.
- **Selective prediction and abstention:** PACE-LM, the risk-adjusted-abstention study.
- **Position.** Not novel in isolation (`docs/research_plan.md` §3.1). The defensible combination is a frozen protocol, a deterministic verifier with a blinded audit, controls and abstention, and the separation of model-written from system-derived claims.

## III. System design (≈ 1 page)
- **A. Testbed and telemetry:**
  - 5 services (gateway, order, inventory, payment, postgres) emitting OpenTelemetry traces, metrics and logs;
  - fault catalog in `docs/fault_catalog.md`;
  - SLO-alert anchoring with baseline and analysis windows.
- **B. E0 dependency-constrained localization:**
  - anomaly evidence per service;
  - ranking constrained by observed caller→callee edges and onset order;
  - fault-type rule;
  - the E0-A-graph ablation, which removes the dependency constraint.

  One small schematic figure (to be drawn by hand; not generated).
- **C. Claim schema and deterministic verifier:**
  - the claim template `predicate(subject, object) = value unit` with cited record ids;
  - checks V0 (schema/unit), V1 (citation exists), V2 (in window), V3 (subject/ownership), V4 (value), V5 (above baseline / onset order), V6 (propagation edge);
  - the `CLIENT A -> B` ownership rule;
  - observed versus inferred claims.

  Reference: `docs/evidence_verifier_spec.md`.
- **D. Abstention:**
  - the pre-registered composite S (sufficiency, agreement, margin, verified coverage) and the margin-only alternative;
  - τ chosen on calibration only (op1, op2).
- **E. LLM conditions (exploratory):**
  - B4: LLM only, with the window rule;
  - E1: E0 ranking plus LLM-written claims, while localization stays E0's;
  - E2: deterministic evidence ownership; canonical claims are system-derived and kept separate;
  - B4-G: the frozen detector gate plus B4, a hybrid;
  - the prompt p4a-v1, the leakage controls, and the equal record budget for B4 and E1.

## IV. Experimental protocol (≈ 0.5 page)
- **Splits.** Dev seed 11 for design; calibration seeds 23/37; test seeds 41/53/67. The test set has 47 fault cases plus 6 controls.
  - One scenario (`41:network_timeout:inventory`) is excluded by the frozen validity rule. The sensitivity analysis with n = 48 changes no conclusion (`docs/final_results.md` §2).
- **Views.** 12 views; R* views use 3 perturbation seeds per case.
- **Metrics:**
  - top-1/top-3 over all fault trials, where a detection failure counts as wrong;
  - fault-type accuracy;
  - control false diagnoses;
  - AURC and the operating points;
  - verifier acceptance, defined as acceptance, not truth;
  - runtime and tokens.
- **Statistics.**
  - The base case is the unit.
  - Wilson intervals, and a cluster bootstrap over base cases (10,000 resamples).
  - Exact McNemar tests with Holm correction.
  - Controls (6 cases) are descriptive only.
- **Reproducibility:**
  - freeze manifest and hash locks;
  - Phase 4B request hashes committed before any call (`b5bf532`);
  - 512/512 calls valid, with byte-identical re-execution of 3 pre-chosen calls;
  - $0 API cost;
  - an 8 GB M1 laptop.

## V. Results

### A. Confirmatory: localization (main result)
- **Figure** `fig_conf_localization` (full width) and **Table** `conf_localization` (or `conf_paired` in the main text, with `conf_localization` in the appendix if space is short).
- **Text:**
  - At C0, E0 scores 0.81 [0.67, 0.90]; B2 0.53, E0-A-graph 0.45, B1 0.26, B3 0.09.
  - In the paired test against B2 (Table `conf_paired`), E0 leads by +0.28 [0.15, 0.40], with 13/0 discordant cases and Holm p = 0.00049.
  - The dependency constraint matters: E0 vs E0-A-graph is +0.36 [0.17, 0.55].
- **Negative findings, stated in the same paragraph:**
  - **R40:** E0 vs B2 is −0.06 [−0.21, 0.08]. E0 loses its advantage under 40 % random record loss.
  - **MT:** E0 drops to 0.32 without traces.
  - **CS:** with root spans removed, E0 rises to 0.94.

### B. Confirmatory: failed fault categories
- **Figure** `fig_conf_fault_types` (one column), or **Table** `conf_fault_types`.
- **Text:**
  - Cascading failure is 0/3 for every method, and E0 does not have it even in its top-3.
  - Strong CPU pressure is 3/9 for E0; E0-A-graph gets 5/9 and B1 4/9. E0 tends to blame the downstream database and pool symptoms.
- Keep both categories in the paper as unsolved.

### C. Confirmatory: abstention
- **Figure** `fig_conf_risk_coverage` (one column), with **Table** `conf_abstention` in the appendix.
- **Text, negative finding first:**
  - The pre-registered composite S (AURC 0.214) is worse than margin only (0.160), with overlapping CIs.
  - Removing verifier coverage (A-ver, 0.204) does not hurt.
  - Margin only at op2 keeps 46 % of diagnoses with 9.5 % risk, against 32.1 % risk without abstention.
  - No control trial was diagnosed (0/108).
  - These are heuristic orderings, not calibrated probabilities.

### D. Confirmatory: verifier behavior on E0's own claims
- **Table** `conf_verifier` (one column; can be shortened to the C0, R40, MT and MM rows).
- **Text:**
  - Acceptance is 89–100 % by view.
  - 265 of 290 rejections are `onset_before` claims, a known E0 citation limitation.
- **Required wording:** "accepted by the verifier". Never say "correct" or "faithful".
- **Precision and recall:** `[AUDIT PENDING]`.

### E. Exploratory: what a local LLM adds
- **Figure** `fig_exp_diagnosis` (full width) and **Table** `exp_diagnosis`; **Table** `exp_views` can go in the appendix.
- **Text:**
  - B4 (LLM only) localizes 74/235 = 0.315, case-mean CI [0.21, 0.42].
  - Its wrong answers concentrate on `inventory` (103 of 160) and `gateway` (42).
  - It solves 0/15 cascade views.
  - E1 reaches 148/235 = 0.630, but only because its localization is E0's and its model is shown E0's ranking. State this in the same sentence as the number.
  - E1 vs B4 is not significant under R40 or MT.
- **Controls:**
  - B4 diagnosed 30/30 healthy-control views and never abstained on its own.
  - E1 under the window rule diagnosed 12/30; under the frozen gate, 0/30.
- **B4-G:**
  - 0/30 control false diagnoses, at the cost of 2/235 missed fault views on test, and 49/342 on dev.
  - It is a hybrid with a deterministic gate, not an LLM-only result.
  - Its call saving (32 calls) is counterfactual.
- **Table** `exp_fault_types` (appendix): B4 types no CPU-pressure fault correctly, and neither does E1; B4 localizes strong CPU 17/45, against E0's 14/45.

### F. Exploratory: verifiability of LLM-written claims
- **Figure** `fig_exp_claims` (full width), plus **Tables** `exp_claims` and `exp_rejections`.
- **Text:**
  - Verifier acceptance is 29.0 % for B4 and 25.4 % for E1, against 94.5 % for E0's templates.
  - Paired by base case, E1 − B4 is −0.039 [−0.094, +0.018], so E1's model writes no more verifiable claims.
  - E1 does write more claims about the true root: +1.51 [0.51, 2.66] verified root claims per case.
  - Nearly all citations are valid ids. Claims fail on subject ownership (V3; 45 % of observed claims), units (V0), copied values (V4) and values not above baseline (V5).
  - Propagation edges: B4 49/608 verified (359 reversed), E1 95/401 (152 reversed).
- **E2** (**Table** `exp_e2`):
  - Of E1's observed claims, 26.9 % are transformed and 38.6 % rejected.
  - 39.0 % of the canonical claims verify, but only 38 concern the root.
  - Canonical claims are system-derived; never count them as model output.
  - E2 shares association rules with the verifier, so its verified rate is not independent evidence.
  - E2 does not change localization.

### G. Human audit of the verifier `[AUDIT PENDING]`
- **Reserve** about 0.3 page and one small table: per condition (E0, E1, B4), the strict and lenient verified-claim precision, the verified-claim error rate, the population-weighted recall and hallucination rate, and verifier–human κ, all with CIs, plus inter-annotator κ and AC1.
- **Source:** `audit/results/audit_analysis.json` and `agreement_A_vs_B.json`, produced only after `unseal`.
- **If the audit is not complete by the deadline:**
  - state that verifier accuracy is unestablished;
  - describe the released audit package;
  - drop every phrasing that implies faithfulness.
- **Caveat to carry:** the E0 rejected stratum is 39/40 `onset_before`.

### H. Cost
- **Table** `exp_runtime` (appendix), or one sentence in the main text.
- **Text:** about 70 s and about 3,400–3,700 prompt tokens per LLM call on an 8 GB M1, 10.4 h for 512 calls and $0, against about 56 ms (rank) plus 128 ms (verify) for E0, both medians.

## VI. Discussion (≈ 0.4 page)
- **Localization belongs to the dependency-constrained deterministic component.** The small LLM alone is near B1 level and collapses onto a few services.
- **The LLM cannot be trusted to abstain or to gate.** Gating must be deterministic, and its missed faults must be reported.
- **Valid citations do not make a claim valid.** Ownership and units dominate the errors, which argues for structured claims plus deterministic checks over free-text "grounding".
- **The verifier's value depends on its accuracy, which is still pending.** Circularity: E0 is checked by rules written alongside it, so the audit is essential.

## VII. Threats to validity and limitations (must stay; ≈ 0.4 page)
1. **One synthetic environment.** Five services on one machine. 47 test faults, with 3 to 12 per type, so the CIs are wide.
2. **Developer bias.** E0 was designed by one developer on 21 dev cases. Test results are held out for the deterministic methods only.
3. **Phase 4 is exploratory and post-study.** The test seeds had already been inspected for E0 before any LLM work. The LLM study is one 4B model, one prompt (p4a-v1) and one run per view. No other model or prompt was compared on test.
4. **Unsolved categories.** Cascading failure (0/3, and 0/15 views) and strong CPU pressure remain unsolved. There are no dual-fault scenarios.
5. **Robustness gaps.** Missing traces; 40 % random record loss. R40 in Phase 4B uses one perturbation seed.
6. **Controls.** Only 6 control cases, so control results are descriptive.
7. **Verifier.**
   - Its accuracy is unestablished until the audit is done `[AUDIT PENDING]`.
   - E2 and the verifier share association rules.
   - E0's `onset_before` citation conflict is a known limitation.
8. **Abstention.** The pre-registered S underperforms the margin, and the verifier-coverage signal adds nothing. No confidence score or risk–coverage curve exists for the LLM.
9. **Baselines.** B2 and B3 are "inspired by" their papers and not validated against the authors' code. There is no external benchmark (RCAEval is not run).
10. **Tooling.** A validator time-attribution artifact excluded one scenario. Three of 97 captures needed a recapture after collector discovery misses.

## VIII. Conclusion (≈ 0.2 page)
Three points: what is confirmed, what is exploratory, and what is pending (the audit). Future work: the post-study fixes recorded in `docs/final_results.md` §9, which were never retro-applied, more seeds, an external benchmark, and other models. Each needs separate approval.

## Page budget (check the ICICYTA 2026 call for the exact limit and template)

| Item | Placement |
|---|---|
| Main text figures | `fig_conf_localization` (full width), `fig_conf_risk_coverage` (one column), `fig_exp_diagnosis` (full width), `fig_exp_claims` (full width); add `fig_conf_fault_types` (one column) if space allows |
| Main text tables | `conf_paired`, `exp_diagnosis`, `exp_claims`, plus the audit table once available |
| Appendix or supplementary material | `conf_localization`, `conf_fault_types`, `conf_abstention`, `conf_verifier`, `exp_views`, `exp_fault_types`, `exp_rejections`, `exp_e2`, `exp_controls`, `exp_runtime` |

**LaTeX packages.** The generated tables need `booktabs`; the figures are PDF (`\includegraphics`).

## Before submission (not done; each needs the user's decision)
- [ ] Complete the human audit: two annotators, adjudication, then `unseal`. Insert §V-G.
- [ ] Read the related work marked "abstract only / snippet only" and adjust the novelty statement.
- [ ] Draw the system schematic (§III-B).
- [ ] Compile the generated tables in the IEEE template. They were not compiled here, because no LaTeX installation is available.
- [ ] Choose a title. Write the abstract and text using the `paper/numbers.tex` macros.
- [ ] Prepare the artifact and data-availability statement: repository, freeze manifest, and audit package (the key released after the audit).
