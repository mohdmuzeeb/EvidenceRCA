# Evidence ownership (E2) and the detector gate (B4-G): design and pre-registered dev evaluation

**Status.** This document, the implementation and `results/phase4a_v3/plan.json` are committed **before any new LLM call** of Phase 4A-v3.

**Scope.** Development only (dev seed 11).

**Preserved.** Nothing below changes any of the following:
- the frozen study: E0, B1/B2/B3, verifier V0–V6, τ and the held-out results;
- any Phase 4A or 4A-v2 artifact. There are two hash locks:
  - `results/phase4a_v2/p4a_v1_preserved.json` (p4a-v1);
  - `results/phase4a_v3/preserved_phase4a_v1_v2.json` (115 files at `5cfee5f`).

**Prompt versions.** p4a-v1 is the primary reference prompt; both E2 and B4-G use it unchanged. p4a-v2 remains a documented development ablation and is not used here.

## 1. Why

In the Phase 4A-v2 pilot, the dominant faithfulness failure under both prompt versions was the subject attribution of claims (V3: 106 and 113 claims). Typically the model named the caller as subject while citing the caller's CLIENT spans to a callee. Instructions and model-declared ownership did not fix this in qwen3:4b.

Ownership is not a judgement call. It is a property of the telemetry: which process emitted a record, what span kind it is, which service it called, its measurement and its unit. E2 therefore takes provenance from the data and never from the model.

The same pilot showed that qwen3:4b never abstains on healthy views. B4-G tests the simplest remedy: put the frozen detector in front of the LLM.

## 2. E2 (separate from E1)

**Pipeline.**
1. E1 under p4a-v1, unchanged: frozen E0 ranking, localization, retrieval and decision, then the LLM's explanation call.
2. Then the deterministic ownership step `phase4/agent/ownership.py`, transform version `own-v1`.

**Provenance.** Provenance is read from immutable View metadata for every cited record: emitter (`service`), span kind, callee (`peer_service` of a CLIENT call), operation, measurement, unit and record family. The model's output cannot set or override any of these.

**Admissible subject per predicate and family.** These are the same association rules as frozen V3, written out as subject derivation:

| Family | Admissible for | Subject |
|---|---|---|
| server span | latency, error_rate | emitter |
| call (CLIENT span to another service) | latency, call_failed, timeout_observed | **callee**: the callers' view of calls to it. Never the caller, and never a claim about the callee's internal work |
| connect span | pool_wait | emitter |
| metric latency_mean / error_ratio | latency / error_rate | emitter |
| metric container_cpu, process_cpu | cpu, no_telemetry | emitter |
| metric pool_checked_out | pool_wait | emitter |
| log | log_error_pattern | emitter |

**Disposition of each original claim.**
- **consistent:** all cited records exist and are admissible, and they imply the claimed subject.
  - A stray `object_service` on a non-relational claim is only flagged (`stray_object_service`). The frozen verifier ignores it for these predicates.
- **transformed:** all cited records exist and are admissible, and they imply **one** subject. But the claim's subject differs, and/or its unit is not admissible for the predicate.
  - A **separate canonical candidate** `cN~canon` is created.
  - Only `subject_service` and `unit` can change, plus a stray `object_service`, which is cleared. Each comes from provenance.
  - Value, predicate, window, citations, kind and measurement never change. Values are **not** rescaled.
  - The audit trail records the transform version, the source claim id, a SHA-256 of the original, every changed field (old → new), the rule applied, the list of unchanged fields and the cited records' provenance.
- **rejected:** a cited record is missing, a cited family is inadmissible for the predicate, or the records imply different subjects. No candidate is created.
- **not_applicable:** `onset_before` (relational) and inferred claims are never transformed.

**Claim sets and verification.**
- The original model claims are kept verbatim, including incorrect subjects, and verified by the frozen verifier exactly as in E1. E2 recomputes this and asserts equality with the stored E1 verification.
- Canonical candidates are verified **separately** by the same frozen verifier, in a separate diagnosis object that holds only the canonical claims.
- Canonical claims are never counted as model claims and never replace an original.

**Reported:**
- original faithfulness: verified / observed original claims;
- canonical faithfulness: verified / canonical candidates;
- transformation rate and rejection rate, both over original observed claims;
- subject-ownership errors: transformed claims with a subject change;
- verified claims per call, original only and original + canonical, kept apart.

**Diagnosis level.** E2's decision, ranking, suspect and fault type are E0's, exactly as in E1. E2 changes claims only.

**Disclosed development step.** Before this commit, I ran the analysis once on the reused dev outputs; no new model call was made. It showed that 41 of 50 "transformations" only cleared a stray `object_service`, which V3 ignores. Those were reclassified as *consistent + flagged*, as above. That is the only change made after looking at dev outputs, and it is dev-seed data.

**Not independent evidence.** Ownership dispositions and V3 share their association rules by construction. Agreement between them (consistent ⇒ V3 pass; transformed or rejected ⇒ V3 fail) is a consistency check, not evidence that either is right.

## 3. B4-G (gated B4; not an LLM-only baseline)

1. Run the frozen shared detector `evidencerca.core.detect_alert` on the view.
2. **No alert:** ABSTAIN, with no LLM call.
3. **Alert:** run the original p4a-v1 B4 prompt and evidence pack, unchanged, on the detector's window.

B4-G's abstentions come from a deterministic detector, so B4-G is reported as a *hybrid* and never as a pure LLM-only baseline.

**Recorded:**
- the gate decision per view;
- false diagnoses on controls;
- missed faults (fault views the gate does not alert on, where B4-G abstains);
- wrong-service diagnoses;
- calls avoided.

## 4. Pre-registered dev evaluation (rules in `phase4/eval/pilot_v3.py`)

- **Views.** The same 20 views as the Phase 4A-v2 pilot (`results/phase4a_v2/pilot_plan.json`), with no reselection: 7 base cases, which are 4 single-service faults, 2 healthy cases and 1 cascade.
- **Comparisons.** E1 vs E2, and B4 vs B4-G. All four use p4a-v1 and the same views.
- **Reuse.**
  - **E2** post-processes E1's p4a-v1 outputs (18 calls).
  - **B4-G** uses B4's p4a-v1 call wherever the detector alerts (16 calls). The pilot used the detector's window exactly in those cases, so the request is identical.
  - Each reused call is listed in `plan.json` with its request, system-prompt, evidence-text and raw-output hashes and the model digest.
  - Reused calls are reported as reused, never as new calls.
- **Live re-execution (the only new calls).** The live E2 and B4-G code paths are re-executed:
  - **E2:** per category, the lowest-index view whose E1 trial made a call (views 0, 4, 8, 13, 16).
  - **B4-G:** per category, the lowest-index view where the detector alerts (views 0, 4, 8, 16; no control alerts).
  - That is 9 calls. Reuse is accepted only if every request hash and raw text equals the pilot's. If any differs, all reused trials are re-run live and reported as such.
- **Windows.**
  - E1/E2 keep the pilot's window rule (alert window, else median-time window), so they are matched with each other and with the v2 pilot.
  - A secondary **gated** reading applies the frozen protocol: no alert means E0 ABSTAIN.
- **Detector coverage beyond the pilot.** The frozen detector runs on every dev-seed-11 primary case × all 18 views (no LLM). This gives coverage and false-alert rates on more than the 20 pilot views.
- **Base-case reporting.** Results are also reported per underlying base case, not only per perturbation view.
- **Resources.**
  - Before the run: AC power and Colima stopped (both checked in code); memory, swap and disk recorded.
  - One inference at a time; local qwen3:4b only; no downloads; $0.

## 5. Human audit (protocol §9 unchanged)

**Dev dry run.** `results/phase4a_v3/audit_dry_run/` tests whether blinded annotators can tell model-written claims from canonical transformations:
- 20 transformed pairs plus 20 consistent original claims as distractors, seed 20261016;
- two sheets, A and B; the two members of a pair always go to *different* sheets, so no sheet shows both;
- items show a canonical rendering and the cited records' metadata only, with no condition labels;
- annotators record a support label and an origin guess (model-written / system-derived / cannot tell);
- the key is sealed.

**Planned analysis after unsealing:**
- origin-guess accuracy against chance;
- support labels within pairs, original vs canonical, using McNemar's test.

**No verifier accuracy is reported** until independent annotation exists, and the verifier's verdicts are never used as human labels.

## 6. Reproduce

```bash
uv run python -m phase4.eval.pilot_v3 plan        # reuse map + re-execution subset (no model)
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &   # Colima stopped
caffeinate -dims uv run python -m phase4.eval.pilot_v3 reverify   # 9 new calls
pkill -f "ollama serve"
uv run python -m phase4.eval.pilot_v3 analyze     # dev_comparison / ownership_analysis / control_gate_results
```
