# Experiment Protocol (Phase 0, pre-registration draft)

Date: 2026-10-11. This protocol is fixed **before** any data is collected. Any later change must be logged in §12 with its date and reason. Thresholds are never tuned on the test split.

---

## 1. Units of analysis
- **Base case**: one fault scenario (or one control scenario) executed once on the testbed. It has one opaque `case_id` (UUID4), which carries no label information.
- **View**: a deterministic transformation of a base case's evidence (`view_id = hash(case_id, perturbation_spec, perturbation_seed)`). Raw telemetry is never modified.
- **Trial**: (method, view). Every method receives the **same** view object for a given (case, condition, perturbation seed).

## 2. Case timeline and ground truth
Every case has a warm-up phase (discarded), a baseline window of 90 s, an injection window of 120 s, a recovery window of 60 s, and a reset/verification step.
- Ground truth is written by the injector and confirmed by an **independent validator** that checks the injected state through a different mechanism from the one used to inject it. Each case is stored in `ground_truth/<case_id>.json` with these fields: `fault_type`, `primary_root_services[]`, `root_operation`, `injection_start/end`, `validator_checks{}`, `propagated_symptom_services[]` (filled in after the case from the validator plus the dependency graph, used for analysis only), `observable_impact` (bool: did the gateway SLO deviate), and the seeds.
- `ground_truth/` sits outside every path that `rca/`, `baselines/` and the view builder can read. A unit test asserts that no RCA module imports or opens anything under it.
- **The RCA window comes from the agent's own alert**, never from the injection time. A deterministic SLO detector on gateway latency and error rate, computed from agent-visible telemetry only, sets `alert_time`. The analysis window is [alert − 60 s, alert + 60 s] and the baseline window is the preceding 90 s. If no alert fires, the case is labelled `not_triggered`, is still evaluated, and is reported separately.
- **Sensitivity analysis**: rerun everything with the window anchored at the injection time. This matches the RCAEval convention and is clearly labelled as oracle-anchored.

## 3. Scenario matrix

| Fault | Eligible targets | Count per seed |
|---|---|---|
| Service crash | order, payment, inventory | 3 |
| Processing latency | gateway, order, payment, inventory | 4 |
| Network timeout | order, payment, inventory | 3 |
| DB pool exhaustion | order, inventory | 2 |
| CPU pressure (capped) | order, payment, inventory | 3 |
| Cascading dependency failure | postgres (root) | 1 |
| **Single-fault subtotal** | | **16** |
| No-fault control (abstention test) | — | 2 |
| Dual-fault (optional) | 4 fixed pairs of independent faults on different services | 4 |
| **Per seed** | | **22** |

- **Seeds**: 6 fixed master seeds `{11, 23, 37, 41, 53, 67}`. Each seed controls the workload RNG, the injection-onset jitter, and the execution order of scenarios within the seed.
- **Splits, by seed** (this prevents near-duplicate leakage between splits):
  - seed 11 = **dev** (debugging, prompt and template development, rule design);
  - seeds 23, 37 = **calibration** (abstention thresholds only);
  - seeds 41, 53, 67 = **test**.
- **Expected sizes**:
  - test: 48 single-fault + 6 control + 12 dual;
  - calibration: 32 single + 4 control + 8 dual.
- Baseline hyperparameters are fixed a priori from the papers (§5). No hyperparameter is tuned on calibration or test data.

## 4. Telemetry conditions (applied to the analysis and baseline windows of every case)

| ID | Condition | Definition (deterministic given `perturbation_seed`) | Perturbation seeds |
|---|---|---|---|
| C0 | Clean | No change | 1 |
| R20 / R40 / R60 | Random record loss | Independently drop each evidence record with probability p ∈ {0.2, 0.4, 0.6}, uniformly over all modalities | 3 each |
| MT / ML / MM | Missing modality | Remove all traces, all logs, or all metrics. Marked `unavailable` | 1 each |
| CS | Critical-span removal | Remove all spans whose service is a primary root, plus their direct child spans. Selected via ground truth by the view builder, which **is** allowed to read ground truth; the RCA methods never see the selection rule | 1 |
| RL | Missing root-cause logs | Remove all log records emitted by primary-root services | 1 |
| J1 / J2 | Timestamp jitter | Add i.i.d. uniform noise ±500 ms (J1) or ±2 s (J2) per record. Parent/child span links are preserved; ordering may break | 1 each |
| CF | Conflicting symptoms | Inject a deterministic decoy: a non-root, non-adjacent service gets synthetic anomalous metric records (CPU and latency shifted by +4 MAD) during the analysis window. The decoy is marked only in the transform log, never in the view | 1 |

- **Views per base case**: 1 + 9 + 3 + 1 + 1 + 2 + 1 = **18**.
- **Interpretation rule**: "R40" means 40 % of *records* were dropped. It does **not** mean 40 % of diagnostic information was lost, because redundancy differs across modalities. For every view we also report the retained fraction per modality, the number of root-service records remaining, and whether any root-cause evidence survived (`root_evidence_present`).

## 5. Methods

| ID | Method | Fixed parameters (a priori) |
|---|---|---|
| B1 | Simple anomaly ranking (robust z of latency, error rate and resources; max per service) | z computed against the baseline window; no thresholds |
| B2 | MicroRCA-inspired | BIRCH (threshold 0.045 as published), α=0.55, PPR c=0.15, reversed edges, host node removed from the ranking |
| B3 | TraceRCA-inspired | δ_fs=10 %, δ_ad=1, δ_spt=10 %, k=100 |
| B4 | LLM-only (same view, same schema) | temperature 0, fixed seed, token budget T (set on dev) |
| E0 | EvidenceRCA, no-LLM | Template claims |
| E1 | EvidenceRCA, LLM (qwen3:4b local) | As B4 |
| E1-P | EvidenceRCA, LLM (paid provider) | **Only if approved** |

**Ablations** (all on the same views):
- **A-ver**: E1 without the verifier. Free to compute, because it reuses the E1 raw LLM outputs.
- **A-graph**: E0 and E1 without dependency-graph constraints. Ranking is unconstrained and propagation edges are not validated. Requires re-running.
- **A-abst**: E0 and E1 with coverage forced to 1 (never abstain). Free to compute.
- **A-margin**: the abstention gate replaced by a single score-margin threshold. This is the required simple competitor (see the research plan, §3.3).
- **A-LLM**: E0 vs E1 is the no-LLM vs LLM comparison.

## 6. Output schema and verification

### 6.1 Diagnosis JSON (every method must emit this; methods without a field emit `null` and the metric is N/A)
`case_id, view_id, method, ranked_candidates[{service, score}], suspected_service, suspected_operation, suspected_fault_type, evidence_ids[], factual_claims[{claim_id, kind: observed|inferred, subject_service, predicate, value, unit, window, evidence_ids[]}], propagation_path[{from, to, evidence_ids[]}], missing_modalities[], uncertainty_reasons[], decision: DIAGNOSE|ABSTAIN`

The `predicate` field takes values from a closed vocabulary: `latency_above_baseline`, `error_rate_above_baseline`, `no_telemetry_from`, `cpu_above_baseline`, `pool_wait_above_baseline`, `log_error_pattern`, `call_failed`, `timeout_observed`, `onset_before`.

### 6.2 Verifier checks (per claim; each check returns pass, fail or not-applicable)
- **V1** Every cited ID exists in the *current view*.
- **V2** The cited record belongs to `case_id` and `view_id`, and lies within the claimed window ± a tolerance (equal to the jitter level for J1/J2).
- **V3** The cited record's service and source match `subject_service`.
- **V4** Numeric value and unit agree with the record (unit-normalised, relative tolerance 5 %).
- **V5** The predicate is supported by the record. For example, `latency_above_baseline` requires the record value to exceed the view's baseline median + 3·MAD, and the verifier computes this itself.
- **V6** Each propagation edge (from→to) exists in the observed dependency graph of the view.

Rules:
- `observed` claims must pass all applicable checks.
- `inferred` claims (causal hypotheses) may not cite evidence as proof of causation. They are verified only for V1–V3 on their premises and are counted separately.
- Failed claims are **rejected and reported**, never rewritten.
- A DIAGNOSE whose suspected service has zero verified observed claims is flagged.

### 6.3 Guarding against circularity in RQ3
1. For E1 and B4, report the unsupported-claim rate **before** filtering (raw LLM output) and **after** filtering.
2. **Human audit**: a stratified random sample of ≥200 claims, across methods and conditions, is labelled supported or unsupported by a human annotator who is blind to method and verifier verdict, using a written rubric. We report the verifier's precision, recall and Cohen's κ against these labels. If a second annotator is available, we also report inter-annotator κ.
3. Report **verified claims per DIAGNOSE**, so that emitting fewer claims is visible rather than rewarded.

## 7. Abstention
- Heuristic score S = a transparent combination (for example the minimum, or a weighted mean, fixed on dev) of four signals:
  - (i) **evidence sufficiency**: the fraction of expected streams for the top candidate that were observed;
  - (ii) **cross-modal agreement**: the number of available modalities whose independent ranking places the same top candidate first, divided by the number of available modalities;
  - (iii) **score margin**: (s1 − s2)/s1;
  - (iv) **verified-claim coverage**: verified observed claims divided by all observed claims.
- S is **not** a probability, and it will not be described as calibrated.
- The threshold τ is chosen on the **calibration** seeds to minimise selective risk subject to coverage ≥ 0.7 (operating point 1), plus a second operating point at selective risk ≤ 0.1 if that is achievable. Both are fixed before the test seeds are opened.
- Risk–coverage curves on the test seeds sweep S and need no threshold. Summary metric: AURC.
- For B1–B4, abstention variants use their own top-1/top-2 score margin, so that the RC curves are comparable.

## 8. Metrics

| Metric | Definition | Notes |
|---|---|---|
| Top-1 / Top-3 service accuracy | Primary root ∈ top-k | Single-fault cases; full coverage |
| Fault-type macro-F1 | Over the 6 fault types | N/A for B2 and B3 (they output no type) |
| All-roots-found@3 | Every primary root ∈ top-3 | Dual-fault cases only; reported separately |
| Coverage | DIAGNOSE / all | — |
| Selective accuracy and risk | Accuracy and error among DIAGNOSE | — |
| AURC | Area under the risk–coverage curve | Lower is better |
| Abstention rate | 1 − coverage | Also reported on controls (desired: ABSTAIN) |
| Unsupported factual-claim rate | Failed observed claims / observed claims | Before and after filtering |
| Valid citation rate | Cited IDs passing V1–V3 / cited IDs | — |
| Runtime p50 / p95 | Wall-clock per trial, excluding capture | Same machine; capture and LLM analysis never run concurrently |
| Tokens and cost | Input/output tokens per trial; cost = tokens × list price at run date | Local model: $0 marginal, with tokens still reported |

If a metric cannot be computed, it is reported as **N/A (reason)**.

## 9. Statistics
- Report n for every cell.
- Proportions: Wilson 95 % CI.
- **Paired method comparisons**: cluster bootstrap over **base cases** (10,000 resamples), because views of the same base case are correlated, giving the CI of the difference. For top-1 at C0, also an exact McNemar test.
- Multiple comparisons: Holm correction within each RQ family.
- Expected precision: ~48 test single-fault cases → Wilson half-width ≈ ±13–14 pp at accuracy 0.5. This is stated as a limitation, and seeds can be added if time allows (each extra seed adds ~22 cases and ~2 h of capture).
- Random-loss conditions: results are averaged over 3 perturbation seeds per base case before bootstrapping.

## 10. Matrix size and compute (estimates)
- **Capture**: 6 seeds × 22 = 132 cases (+ ~12 smoke cases) ≈ 144 × ~5 min ≈ **12 h**.
- **Deterministic trials**: 132 × 18 views × (B1, B2, B3, E0, A-graph-E0) ≈ 11.9k trials. Minutes of compute.
- **LLM trials, full matrix**: 132 × 18 × (B4, E1, A-graph-E1) ≈ 7.1k calls. Too slow for a local 4B model.
- **Proposed core LLM matrix** (for your approval): calibration and test seeds only (110 cases) × 7 views {C0, R40 (1 perturbation seed), MT, ML, MM, RL, CF} × (B4, E1, A-graph-E1) ≈ **2.3k calls**. At an assumed 20–60 s per call, that is ≈ **13–39 h** on the local model. The timing assumption will be checked by a 5-call benchmark in Phase 4 before committing.
- **Optional paid-model subset**: E1-P and B4-P on test seeds × {C0, R40, CF} ≈ 66 × 3 × 2 ≈ 400 calls (~2.3 M input + 0.3 M output tokens). Cost to be computed from list prices at approval time.

## 11. Reproducibility record (written per run)
`run_manifest.json` records:
- git commit hash and dirty flag;
- `uv.lock` hash;
- Python version;
- OS build, CPU model and RAM;
- Docker, Compose and Colima versions plus the container image digests;
- Ollama version and model digest;
- the provider and model ID;
- all master and perturbation seeds;
- the config file hash;
- start and end timestamps;
- the sha256 of every raw case bundle.

Raw bundles are written once and set read-only. Views are recomputed from raw + spec rather than stored. Only per-trial outputs are written to `results/raw/`; aggregates go to `results/processed/`, and figures are regenerated from those by script.

## 12. Deviation log

All entries below were recorded on 2026-10-12, before the Phase 2 pilot was captured. No final (calibration or test) data had been collected.

| Date | Change | Reason |
|---|---|---|
| 2026-10-12 | **Warm-up fixed at 30 s.** It was unspecified before. | Covers pool connection setup and first-request cold start (Phase 1: the first order took 89 ms vs ~13 ms warm). |
| 2026-10-12 | **Onset jitter: uniform 0–10 s** added to the 90 s baseline, drawn from the scenario's seed. | Makes "seed controls injection-onset jitter" concrete; keeps onset times from being a fixed offset. |
| 2026-10-12 | **Fault parameters fixed:** processing delay +500 ms (below the 2 s caller timeout); netem egress delay 2500 ms (above it); pool exhaustion holds 5/5 connections in open transactions; CPU pressure uses 2 busy loops in a 1.0-CPU-capped container; the cascade is an ACCESS EXCLUSIVE lock on `items`. | Needed to implement §3. Chosen so that each fault is unambiguous in kind (latency vs timeout) and bounded by the existing limits. |
| 2026-10-12 | **Injection start/end = the mechanism's own VM-clock timestamp** (Docker state times, the control-channel clock, VM-host `date`, Postgres `clock_timestamp()`). The injection window lasts 120 s from that start, and recovery 60 s from the cleanup timestamp. | Item 4 of the Phase 2 instructions: use VM-clock timestamps. Host↔VM offsets are recorded per case. |
| 2026-10-12 | **Raw and ground-truth locations:** `data/cases/<case_id>/` (raw, sealed read-only with a hash manifest) and `data/ground_truth/<case_id>/` (sibling root). They replace `results/raw/<case_id>/` and `ground_truth/<case_id>.json`. | Keeps large raw data out of git and makes the separation enforceable by an allowlist (`evidencerca/rca_input.py`). |
| 2026-10-12 | **Independent validator** made concrete per fault (`docs/fault_catalog.md`). Every detector is evaluated in every case, and each fault must be absent before injection and after recovery. | §2 required a validator "through a different mechanism"; this pins down which one. |
| 2026-10-12 | **Observable impact** is defined on the workload's client-side records: unexpected-outcome rate +5 pp, or p95 ×2, in steady injection vs baseline. | §2 said "gateway SLO deviated" without thresholds. It is used for reporting only, never by RCA methods. The agent's own alert detector (§2) is unaffected and is still to be built in Phase 3. |
| 2026-10-12 | **Workload:** open-loop Poisson at 10 req/s, max 32 in flight (excess arrivals shed and recorded). Mix: 35 % stock lookups, 45 % orders, 10 % order lookups, 10 % invalid orders (unknown item → 404, insufficient stock → 409, quantity 0 → 422). The workload seed is derived from (master seed, scenario). | §4.1 of the research plan specified a seeded Poisson mix without numbers. |
| 2026-10-12 | **Seed stock raised** to 1,000,000 for sku-001/002/003/005 (sku-004 stays at 50 for insufficient-stock rejections). | 10 req/s for ~5 min would otherwise exhaust stock and turn every case into a 409 storm. |
| 2026-10-12 | **Pilot targets drawn by a seeded rule** (`harness/schedule.py: pilot()`) from the seed-11 matrix. Mechanism checks use fixed, different targets and a shorter timeline (15/40/45/40 s), and are **not** experiment data. | Pilot scope from the Phase 2 instructions. |
| 2026-10-13 | **CPU-pressure intensity variant `strong`** added (Phase 3): during injection the target's own CFS quota is lowered from 1.0 to 0.2 CPU (`docker update --cpus 0.2`), with the same 2 busy loops, and is restored exactly afterwards. Independent validation uses the cgroup throttled-period ratio ≥ 0.5. The original (`standard`) variant and its classification are unchanged, and its cases are kept and reported. | The standard variant produced no SLO deviation and no agent alert in 3/3 dev cases (§13). |
| 2026-10-13 | **Evidence normalization:** DB `connect` spans are local to the caller's pool (not calls to postgres); cumulative app metrics are aggregated across attribute series per export (request-weighted); Postgres log level and millisecond time are parsed from the text. | Correctness fixes found on dev data (docs/phase3_validation.md §7). |
| 2026-10-13 | **Alert detector fixed:** 5 s buckets; reference = buckets in [t−90 s, t−30 s), excluding the first 30 s of capture; alert when gateway error fraction ≥ median + max(0.05, 3·MAD) or p95 ≥ max(2·median, median + 3·MAD + 50 ms) in 3 consecutive buckets; alert time = start of the first. Falls back to gateway app metrics when traces are unavailable. Baseline window clipped to skip the first 30 s of capture. | §2 required a deterministic detector without parameters. |
| 2026-10-13 | **View specifics:** CS for a postgres root removes DB statement spans (postgres emits none); the CF decoy is a non-root, non-adjacent app service (seeded), shifted +4·MAD on container CPU, process CPU and latency_mean in the agent's analysis window (oracle window if the clean view does not alert); J1/J2 set the view's time tolerance. | Needed to implement §4. |
| 2026-10-13 | **Claim schema extension:** optional `object_service` for the relational predicate `onset_before`. | §6.1's schema had no field for the second service. |
| 2026-10-13 | **Dev case set:** pilot run A + the dev-seed matrix + the 3 strong-CPU dev cases (21 base cases). Pilot run B is used only as a replicate. | — |


## 13. Freeze candidate (prepared 2026-10-13, Phase 3): decisions required before calibration/test capture

Everything not listed here is frozen as implemented at the Phase 3 commit: detector, signals, E0 rules, baselines, verifier, views and metrics. These were developed on dev seed 11 only; no calibration or test case exists.

| # | Open decision | Dev evidence (seed 11 only, small n) | Recommendation |
|---|---|---|---|
| 1 | CPU-pressure intensity for the matrix | Standard: 0/3 alerts, p95 +23–35 ms. Strong: 3/3 alerts, p95 268–4160 ms, with less host CPU than standard. | Use `strong` in the calibration/test matrix. Report the standard-variant dev cases as a documented detection-failure category. |
| 2 | Verifier V4 association for `onset_before` | A cited CLIENT span (caller → subject) is associated with both services. V4 then attributes it to the object and rejects a correct lead (27 V4 rejections, all onset). | Count a cited record towards the object only if the object emitted it and it is not a call to the subject. Freeze **before** test capture; do not change after. |
| 3 | Abstention score | Dev AURC: S (equal-weight 4-signal) 0.231 vs A-margin 0.142 for E0. The composite is *worse* than margin alone on dev. | Keep S as pre-registered, with A-margin as the mandated competitor, and report both. Do **not** re-weight on 16 dev cases (overfitting risk), unless you prefer to fix weights on dev now. |
| 4 | τ | Not chosen (protocol: calibration seeds only) | Choose on seeds 23/37 per §7 |
