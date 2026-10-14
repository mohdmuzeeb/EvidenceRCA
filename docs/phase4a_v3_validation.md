# Phase 4A-v3 validation: E2 (deterministic evidence ownership) and B4-G (detector gate)

**Scope.** Development only (dev seed 11). The design was pre-registered in `9a8a944` (`docs/evidence_ownership_design.md`) before any new call. The frozen study, Phase 4A (p4a-v1) and 4A-v2 artifacts are unchanged; the freeze manifest and both hash locks verify. Phase 4B has **not** been started.

**Verdict.**
- **E2:** deterministic ownership turns 28 of the model's 102 observed claims into canonical candidates; 13 of those verify (46 %).
  - Verified claims per call, counting original and canonical claims separately, go from 1.39 (E1) to 2.11.
  - Original model faithfulness is unchanged at 25/102 (24.5 %), because originals are untouched.
  - Another 39 claims (38 %) cannot be repaired from provenance and are rejected.
  - Only 6 canonical claims concern the true root.
- **B4-G:** removes every false diagnosis on the 4 pilot controls (3 → 0) and misses no pilot fault. Across all 378 dev views, however, the gate misses 49 of 342 fault views (14.3 %). All 49 belong to the three standard-strength CPU-pressure cases.
- **Unchanged:** neither variant changes localization. E2 = E1 = E0's top-1 (10/16), and B4-G = B4 (2/16) on fault views.

## 1. What ran, and which calls are reused

| | E1 | E2 | B4 | B4-G |
|---|---|---|---|---|
| Views (base cases) | 20 (7) | 20 (7) | 20 (7) | 20 (7) |
| LLM calls used | 18 | 18 (the same calls as E1) | 20 | 16 (B4's calls on the views where the gate alerts) |
| Source of calls | v2 pilot, p4a-v1 | reused | v2 pilot, p4a-v1 | reused |

**Reused calls.**
- E2 and B4-G add **no** new trial calls. They reuse the Phase 4A-v2 pilot's p4a-v1 calls, each listed in `results/phase4a_v3/plan.json` with its request hash, system-prompt hash, evidence-text hash, raw-output hash and model digest.
- E2 re-verifies E1's stored claims with the frozen verifier, and the analysis asserts that the result is identical to the stored verification.

**New calls this phase: 9.** These were the pre-registered live re-executions through the new E2 and B4-G code paths, covering views 0/4/8/13/16 for E2 and 0/4/8/16 for B4-G.
- All 9 were ok, with the **same request hash and byte-identical raw text** as the pilot.
- Reuse was therefore accepted (`results/phase4a_v3/reuse_verification.json`).

**Resources.**
- Before the run: AC power, Colima stopped, disk 21 GB free, swap 5.6 / 6.1 GB, memory-pressure free 40 %.
- During the run: Ollama peak RSS 3.63 GB. Per-call wall time 59.7–89.8 s, mean 68.4 s.
- After the run: swap 9.7 / 11.3 GB, memory-pressure free 32 %.
- One inference at a time; Ollama stopped afterwards. Cost $0.

## 2. E1 vs E2

| | E1 | E2 |
|---|---|---|
| Top-1 on fault views, 16 views from 5 base cases (E0's) | 10 | 10 |
| Fault-type correct (E0's) | 8 | 8 |
| False diagnoses on controls: pilot window rule / gated (frozen protocol) | 2 / 0 | 2 / 0 |
| Original observed claims (model-written) | 102 | 102, unchanged |
| Original verified | 25 (24.5 %) | 25 (24.5 %) |
| Canonical candidates (system-derived, kept separate) | – | 28 |
| Canonical verified | – | 13 (46.4 %) |
| Verified per call: original only / original + canonical | 1.39 | 1.39 / 2.11 |
| Mean wall time (LLM) | 58.5 s | 58.5 s (no extra call; the ownership step is deterministic) |

**Ownership dispositions** for the 102 original observed claims (`results/phase4a_v3/ownership_analysis.json`):
- **consistent: 35.** All pass V3. 25 verify; the other 10 fail V4 (7) or V5 (3).
  - 19 of the 35 carried a stray `object_service`. It is flagged, not transformed.
- **transformed: 28 (27.5 %).**
  - 24 are **subject-ownership errors**: almost all caller → callee for cited CLIENT calls.
  - 7 had a unit change (the value is not rescaled).
  - None of the 28 originals verified (21 fail V3; 7 fail V0 before V3).
  - Of their canonical versions, 13 verify, 11 fail V4 (values unchanged) and 4 fail V5.
- **rejected: 39 (38.2 %).**
  - 31 cite a record family the predicate does not admit, e.g. error_ratio metrics for a latency claim, or connect spans for latency.
  - 8 cite records that imply different subjects.
  - No candidate is created for these.
- **not applicable: 2** (inferred claims).

**Relevance caveat.** Only 6 of 28 canonical candidates (5 verified) concern the true root service. Most canonical claims are correct statements about *other* services, e.g. calls to inventory or postgres being slow. More verified claims per call therefore does not mean better-focused explanations.

**Not independent evidence.** Ownership dispositions and V3 share their association rules, so their perfect agreement (consistent ⇒ V3 pass; transformed/rejected ⇒ V3/V0 fail) is a consistency check only. Whether canonical claims are actually *supported*, and whether readers can tell them apart from model claims, needs the human audit (§5).

**By base case (E2), original verified / observed, canonical verified / candidates:**

| Base case | Views | Original | Canonical |
|---|---|---|---|
| network_timeout:order | C0, MT, CF | 5/18 | 4/7 |
| processing_latency:order | C0, MT, CF | 11/18 | 1/3 |
| service_crash:payment | C0, MT, CF | 0/18 | 6/8 |
| db_pool_exhaustion:inventory | C0, MT, CF | 6/12 | 0/1 |
| healthy-0 | C0, R40 | 3/6 | 1/2 |
| healthy-1 | C0, R40 | 0/6 | 1/1 |
| cascading_failure:postgres | C0, R40, MT, CF | 0/24 | 0/6 |

- Gains concentrate in two base cases (service_crash:payment and network_timeout:order).
- The cascade case gains nothing: all six of its canonical candidates fail V4/V5.

## 3. B4 vs B4-G

**Pilot views** (`results/phase4a_v3/control_gate_results.json`):

| | B4 | B4-G |
|---|---|---|
| Gate alerts: fault views / controls | – | 16/16 / 0/4 |
| False diagnoses on 4 controls | 3, plus 1 malformed (truncated) answer | **0** |
| Missed faults (gate silent on a fault view) | – | 0 |
| LLM calls | 20 | 16 (4 avoided) |
| Top-1 on fault views | 2/16 | 2/16 |
| Wrong-service diagnoses on fault views | 14 | 14 |
| Fault-type correct | 4/16 | 4/16 |
| Verified claims per call | 1.65 | 1.88 (controls removed) |
| Mean wall time per call | 66.4 s | 64.1 s |

**Detector coverage on all dev views** (21 base cases × 18 views = 378, no LLM):

| | |
|---|---|
| Fault views alerted | 293/342 = 85.7 % |
| Missed fault views | 49 = **all** views of the 3 standard-strength `cpu_pressure` cases (payment 18/18 missed, inventory 16/18, order 15/18) |
| Fault coverage excluding those 3 cases | 288/288 |
| Healthy views with a false alert | 0/36 |

B4-G would abstain on those 49 views. The calibration and test seeds use only the strong CPU variant, which the dev data shows is detected (its 3 cases × 18 views all alert). The dev result does not establish coverage on the held-out splits, and I did not open them.

**Reading.**
- The gate fixes healthy-view false diagnoses, which is a detection problem. It does nothing for localization, which is B4's real weakness: 2/16, and 14 wrong-service diagnoses.
- B4-G is a hybrid. Its abstentions come from the frozen deterministic detector, and it must not be reported as an LLM-only baseline.

## 4. Regressions and limitations

**No diagnosis-level regressions.** E2 and E1 are identical by construction, and B4-G equals B4 on every alerted view.

**Risks found:**
- **Claim volume vs focus.** E2's added verified claims are mostly about non-root services (22 of 28 candidates).
- **Low transform yield.** E2 cannot repair 38 % of claims (rejected) or 11 V4 value errors, because it never edits values.
- **Gate misses.** B4-G misses weak faults the detector cannot see (14.3 % of dev fault views, all standard CPU pressure).

**Sample size.** 20 views from 7 base cases: 4 faults with 3 views each, 2 healthy cases with 2 each, 1 cascade with 4. Per-view counts are not independent. All results are descriptive.

**Disclosed step.** One design change was made after a no-call dev trial of the analysis: a stray `object_service` is flagged instead of transformed. Without it, 41 of 50 "transformations" would have been cosmetic.

**Verifier verdicts are not ground truth.** No verifier accuracy is claimed.

## 5. Human audit dry run (protocol §9 unchanged)

**Sheets.** `results/phase4a_v3/audit_dry_run/`: sheets A and B, 30 items each.
- 20 transformed pairs; the original and canonical member of each pair are always on different sheets.
- 20 consistent model claims as distractors.
- Each item shows a canonical rendering and the cited records' metadata, with no condition or origin labels.
- Annotators record a support label and an origin guess. The key is sealed; seed 20261016.

**Planned analysis after unsealing:**
- origin-guess accuracy against chance;
- original-vs-canonical support within pairs (McNemar).

## 6. Implications for Phase 4B (decision is yours; nothing has been run)

**E2.**
- If adopted, report it alongside E1 with the two claim sets kept apart: original model faithfulness and canonical faithfulness.
- Add relevance: the share of canonical claims about E0's suspect.
- It costs no extra calls.

**B4-G.**
- If adopted, report it next to B4 (not instead of it), with gate coverage per split.
- It cuts calls on healthy views and removes their false diagnoses.

**Not addressed by either variant:**
- localization by the LLM (B4 2/16);
- value errors (V4);
- non-admissible evidence choices (38 % rejected).

## 7. Files

- `results/phase4a_v3/dev_comparison.json`: per condition, category, base case and trial.
- `results/phase4a_v3/ownership_analysis.json`: every claim's provenance, disposition, changes, audit trail, and original and canonical verifier checks.
- `results/phase4a_v3/control_gate_results.json`: gate per view, B4 vs B4-G, and dev-wide detector coverage.
- `results/phase4a_v3/plan.json`, `reuse_verification.json`, `raw_reverify/` (9 new calls), `audit_dry_run/`, `preserved_phase4a_v1_v2.json`, `run_logs/`.
