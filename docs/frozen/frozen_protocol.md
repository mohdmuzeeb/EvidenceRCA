# Frozen Experiment Protocol (calibration and held-out test)

This was frozen on 2026-10-13, after your approval of protocol §13 (decisions 1–4).
- **Machine-checked manifest:** [`freeze_manifest.json`](freeze_manifest.json), with the SHA-256 of every file that determines capture or evaluation.
- **Enforcement:** `python -m evaluation.freeze verify` runs before any calibration/test capture and before both final-evaluation stages, and refuses to run on any change.
- **Freeze commit:** recorded in [`FREEZE_COMMIT`](FREEZE_COMMIT).

The full protocol is `docs/experiment_protocol.md` (§§1–11, deviations §12, decisions §13). This page summarizes what is frozen.

## Seeds and splits (fixed in Phase 0, unchanged)

| Split | Master seeds | Use | Labels read by |
|---|---|---|---|
| dev | 11 | algorithm development (Phases 2–3; done) | development scripts |
| calibration | 23, 37 | abstention threshold τ **only** | `evaluation/run_final.py calibrate` |
| test | 41, 53, 67 | held-out evaluation | `evaluation/run_final.py evaluate`, only after calibration is written; it records the calibration file's hash |

## Capture

- **Scenarios per seed:** 16 single-fault + 2 controls = 18. Target lists follow protocol §3; the execution order is seeded. The lists, in execution order, are in the manifest.
- **CPU pressure:** the `strong` variant for calibration/test (decision 1). The dev standard-variant cases stay as documented detection failures; they are neither discarded nor relabelled.
- **Dual-fault scenarios:** not captured. They are optional in protocol §3 and not implemented. Expected sizes are therefore test 48 single + 6 controls, and calibration 32 single + 4 controls.
- **Timeline:** 30 s warm-up, 90 s baseline + 0–10 s seeded onset jitter, 120 s injection, 60 s recovery.
- **Workload:** 10 req/s Poisson, at most 32 in flight, a seeded schedule identical across variants.
- **Fault parameters:** as in `docs/fault_catalog.md` and the manifest.
- **Validity:** every case is validated independently:
  - the fault occurred (independent detector);
  - recovery;
  - telemetry completeness;
  - capture continuity (sleep/stall);
  - no monitor errors.
- **Invalid captures:** recorded with their reason and recaptured with the same configuration. A valid but difficult case (for example, no alert fires) is **never** discarded.
- **Capture conditions:** Docker only (Ollama not running), AC power, lid open, `caffeinate -dims`. Analysis never runs during capture.

## Methods, verifier, abstention (frozen code, hashes in the manifest)

- **Detector and windows:** protocol §12 (2026-10-13 rows).
- **Views:** 18 per base case (C0, R20/R40/R60 × 3, MT, ML, MM, CS, RL, J1, J2, CF).
- **Methods:** B1, B2 (MicroRCA-inspired), B3 (TraceRCA-inspired), E0, and E0-A-graph, all on the same view and windows. Both anchors are run: alert (primary) and oracle (sensitivity).
- **Verifier V0–V6:** includes the approved `onset_before` association correction (decision 2). No further verifier changes are allowed based on calibration or test outcomes.
- **Abstention:** S = the equal-weight mean of sufficiency, modality agreement, margin and verified-claim coverage (decision 3; weights not optimized). Ablations:
  - A-margin (margin only, the mandated competitor);
  - A-ver (S without coverage);
  - A-abst (never abstain);
  - A-graph.
- **τ (decision 4):** chosen on the calibration seeds only, from alert-anchored diagnosed fault trials pooled over all views:
  - **op1:** minimum selective risk subject to coverage ≥ 0.7;
  - **op2:** maximum coverage subject to selective risk ≤ 0.1, if achievable.

  Computed for each score: E0:S, E0:A-margin, E0:A-ver, E0-A-graph:S, B1/B2/B3 margins.

## Metrics and statistics (protocol §§8–9)

- **Localization:** top-1 and top-3 over triggered trials, plus over all fault trials, where a detection failure counts as wrong.
- **Detection coverage.**
- **Controls:** alerts and diagnoses.
- **Fault-type accuracy:** E0.
- **Selective risk and coverage** at op1/op2, plus AURC.
- **Verification:** verified-claim rate, verified claims per DIAGNOSE, rejection breakdown, propagation edges.
- **Breakdowns:** per fault type and per missingness condition.
- **Runtime.**
- **CIs:**
  - Wilson for single-view proportions;
  - for R20/R40/R60, the mean over 3 perturbation seeds per base case, with a cluster bootstrap over base cases (10,000 resamples);
  - AURC, with a cluster bootstrap over base cases.
- **Paired comparisons** (E0 vs B1, B2, B3, E0-A-graph): the top-1 difference with a cluster-bootstrap CI per condition; exact McNemar at C0; Holm correction within each condition's family.
- **Raw per-trial predictions and abstention decisions** are kept in `results/final/*_trials.jsonl`.

**Not allowed after viewing test results:** any change to code, parameters, thresholds or rules. If an error is found after that point, it is reported as an error; it is not silently fixed and re-run.
