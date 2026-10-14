# Prompt revision p4a-v1 → p4a-v2: rationale and pre-registered pilot

**Status.** This document, the p4a-v2 prompt/schema and the pilot plan were written and committed **before any p4a-v2 model call**. A later section, "Observed results", links to `docs/phase4a_v2_validation.md`, which is written after the pilot. Nothing above that line is edited after the run.

**Scope.** Development-only (dev seed 11). No calibration or test case is opened. The frozen study and the Phase 4A artifacts are untouched:
- frozen study: commit 912085c, calibration fb6a9fd, held-out results 7b54850;
- Phase 4A (p4a-v1): commit eeb18fc.

The Phase 4A artifacts are protected in two ways:
- `results/phase4a_v2/p4a_v1_preserved.json` holds the SHA-256 of all 21 p4a-v1 files (code, prompt, benchmark, raw calls, validation doc).
- `tests/test_phase4_v2.py::test_p4a_v1_preserved` fails if any of them changes.

## 1. What is changed, and what is not

| | p4a-v1 (kept, unchanged) | p4a-v2 (new, separate files) |
|---|---|---|
| Prompt | `phase4/agent/prompts.py` | `phase4/agent/prompts_v2.py` |
| Schema / validator | `phase4/agent/output_schema.py` | `phase4/agent/output_schema_v2.py` (schema version `p4a-v2-typed`) |
| Conditions | `phase4/agent/conditions.py` | `phase4/agent/conditions_v2.py` |
| Evidence pack (digest, graph, record table) | `phase4/agent/evidence_pack.py` | **the same module and the same rendering function**; v1 and v2 see byte-identical case text for a view and condition, and the pilot checks this per pair |
| Model and decoding | qwen3:4b, digest 359d7dd4…, temperature 0, seed 20261014, num_ctx 6144, num_predict 1024, thinking off, timeout 300 s, ≤ 1 transport retry | identical (`LLMSettings()`) |
| E0, B1/B2/B3, verifier V0–V6, τ, abstention score S | frozen | frozen, not modified, not re-tuned |

Only the instructions, the output schema and the agent-side typed validation differ, so the comparison isolates the prompt/schema revision.

## 2. Failure modes observed in Phase 4A and the change made for each

The Phase 4A five-call benchmark (dev seed 11) is the only evidence used to design v2. No other model output was inspected.

| Phase 4A observation | Verifier check | p4a-v2 change |
|---|---|---|
| Counts written as `1` with unit `1` (4 claims: call_failed, log_error_pattern used as yes/no) | V0 | **Typed claims.** One schema branch per predicate fixes the measure class (count / duration / ratio / resource) and the allowed units. A count predicate can only carry `{call}` or `{record}`. Grammar-constrained decoding enforces this, and our validator re-checks it. The prompt says "counts are whole numbers copied from the summary, never 1 as a yes/no flag". |
| Caller named as subject while citing its CLIENT spans (11 claims) | V3 | **Ownership.** Every citation is `{id, emitter, record}`. The prompt explains SERVER and CLIENT spans: a call record is the caller's view of a dependency call and supports claims only about the callee, never the callee's internal work or the caller's own work. A typed check compares each declared emitter and record type with the actual record, and checks predicate × record-type × subject admissibility. |
| Miscopied values (median of several records, wrong field) | V4 | The value rule is restated as the frozen verifier computes it: cite 1 record and copy its value, or 3 and copy the middle one (the verifier takes `median_high`). The citation limit drops from 5 to 3. |
| Claims about services not above their own baseline | V5 | Make an observed claim only if the summary shows the analysis window clearly above that family's baseline and each cited record is far above the baseline median (unchanged in intent; reworded). |
| Propagation edges mostly written caller → callee (7 of 9 edges rejected) | V6 | Direction defined as *suspected root cause → affected dependent*, with a worked example. Invocation (caller → callee) and impact (callee → caller) are distinguished. A typed check validates each edge against the observed call graph and names reversals. An empty path is explicitly acceptable when the direction is uncertain. |
| B4 never abstained; it diagnosed the healthy control | (decision) | **Abstention.** The model writes `evidence_strength` (clear_anomaly / weak_or_no_deviation / conflicting) before the decision; keys are generated in schema order. DIAGNOSE is allowed only with clear_anomaly. Normal variation is described: single slow records, the window maximum, small shifts. ABSTAIN with suspected_service "none" is explicitly allowed. The prompt says that telemetry being present is not a reason to diagnose. No numeric threshold, learned or otherwise, is introduced; the examples are qualitative ("several times its baseline median"). |
| E1's own judgement was only an `agrees_with_e0_top1` flag | — | The E1 task asks for an independent assessment. The model's decision is recorded as its **abstention recommendation**, with `recommends_abstain` and `names_other_service`. E1's diagnosis-level outcome stays E0's. |

**Label hygiene.** The v2 instruction text is constant across cases. It contains no label token except the closed output vocabulary line; this is tested. The leakage scan over all 38 pilot prompts found 0 hits (`results/phase4a_v2/structured_output_tests_v2.json`).

## 3. Typed validation and its relation to the frozen verifier

`output_schema_v2.typed_checks` runs on agent-visible data only: the record table and the call graph the model was shown. It checks:
- unit / measure / predicate consistency;
- that the declared emitter and record type match the record;
- that the record type is admissible for the predicate;
- the subject association (a call record's subject must be its callee; other records must be emitted by the subject);
- `onset_before` structure (subject record first, object record second);
- that inferred premises are associated with the subject;
- that each edge exists in the observed graph in the stated direction, with call evidence `to → from`.

It never checks values or baselines (V4/V5) and never edits a claim.

**Typed rejection does not remove anything.**
- A typed-rejected claim stays in `pre_filter` and **is still sent to the frozen verifier**.
- `post_filter` = typed-passed ∧ verifier-verified observed claims.
- Agreement between the typed checks and V3/V6 is expected by construction. It is *not* evidence that either is correct. Only the human audit (§6) can show that.

## 4. Pre-registered pilot (case-selection rule fixed in code)

`phase4/eval/pilot_v2.py`, `make_plan()`; the output is `results/phase4a_v2/pilot_plan.json`, committed with this document. The rule uses scenario metadata only, never any method's performance:

- **straightforward:** single-service, non-"strong" fault cases (excluding the cascading and healthy cases), ordered by `sha256("p4a-v2-pilot|" + scenario key)`. The first case of each new fault type is taken until 4 types are covered. View C0.
  - Result: network_timeout:order, processing_latency:order, service_crash:payment, db_pool_exhaustion:inventory.
- **missing_trace:** the same 4 cases, view MT.
- **conflicting:** the same 4 cases, view CF. A decoy service that is neither a root nor adjacent to one has its CPU and latency metrics shifted by 4 MAD in the analysis window.
- **control:** both healthy dev cases × views C0 and R40 (seed 1). The correct answer is ABSTAIN.
- **cascade:** the only dev cascading case (postgres) × views C0, R40 seed 1, MT and CF. Only one dev cascade exists, so the four cascade views are not independent cases.

**Calls.**
- 20 views × {B4, E1} × {p4a-v1, p4a-v2} gives at most 80 calls. E1 makes no call when E0 has no candidate.
- Order: view by view, B4 then E1. The version order alternates per view (ABBA), which limits drift confounds such as memory pressure and thermals.

**Window rule (label-free, identical for all).** Use the detector's alert window if it fires, else a window anchored at the median record timestamp.

**Reproducibility check.** Two pilot v1 requests are byte-identical to Phase 4A benchmark calls:
- healthy C0 B4: request hash identical, confirmed by a mock dry run;
- cascade C0 E1.

Identical raw text is expected at temperature 0 with a fixed seed; any difference will be reported.

**Resources.**
- Preconditions: AC power, Colima stopped (both checked in code), disk/memory/swap recorded.
- One inference at a time (`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`).
- Local qwen3:4b only; no download, no paid API.

## 5. Metrics and pre-declared reading

**Reported per prompt version × condition, overall and per category:**
- calls and call outcomes; parse status; truncation;
- raw observed/inferred claims; verifier-verified observed claims (rate and per call); typed-rejected claims; post-filter claims;
- first failed verifier check per claim; V4 failures that are display-precision artifacts;
- citations with a known alias, and declared ownership correct (v2);
- edges raw, verified and reversed;
- verifier "DIAGNOSE without a verified suspect claim" flags;
- decisions: diagnosis-level, and the LLM's own;
- control false diagnoses; over-abstention on fault views;
- top-1 accuracy (B4 decision; E1's LLM pick; E1/E0 diagnosis);
- fault-type accuracy;
- latency, prompt and output tokens.

**Pre-declared reading.** The pilot is descriptive: n = 20 views, one seed, one cascade case, so no significance test is run.

p4a-v2 counts as **improving claim faithfulness** if, pooled over B4 and E1:
- its verifier-verified rate among observed claims is higher than p4a-v1's;
- V0 and V3 failures and reversed edges are lower;
- verified claims per call do not fall.

p4a-v2 counts as **improving control abstention** if its B4 decisions and its E1 recommendations on the 4 control views diagnose less often than p4a-v1's.

**Regressions to be reported even if the above hold:**
- more abstention on fault views;
- lower top-1 or fault-type accuracy;
- more schema failures or truncation (the typed schema is longer, and num_predict is unchanged);
- higher latency or token use;
- typed checks rejecting claims the verifier verifies.

**Known confound, fixed before the run.** The record table renders trace values to 0.1 ms, metrics to 4 significant digits. V4 recomputes from the exact value with 5 % tolerance, so small values can fail V4 even when copied correctly. These cases are counted separately (`v4_value_matches_rendered_precision`). The pack is not changed in this iteration; changing it would break identical evidence with p4a-v1.

## 6. Human audit preparation (sampling plan recorded before Phase 4B)

**Dev pilot instrument dry run.** `pilot_v2.audit` produces `results/phase4a_v2/audit/audit_sheet_dev_pilot.csv`:
- a target of 200 claims, drawn from all pilot claims, both pre- and post-filter;
- balanced over the 4 strata {p4a-v1, p4a-v2} × {B4, E1}, 50 each;
- within each stratum, half verifier-verified and half rejected, with any shortfall filled from the other half and recorded;
- at least 4 claims per predicate group when available;
- seed 20261015.

**Blinding.** Each item shows only:
- a canonical rendering of the claim;
- the cited records as displayed to the model, with selection tags removed;
- up to 30 same-family baseline and analysis records of the claimed service;
- the observed call edges;
- empty label columns: supported / unsupported / partially_supported / not_assessable.

It hides:
- prompt version, condition, the model's wording and explanation;
- verifier verdicts and statistics, and typed-check verdicts;
- case/view ids and ground truth.

The key is in `audit_key_dev_pilot.SEALED.json`.

**Use.** This dev sheet is used for:
- rubric piloting;
- the 10 dev worked examples that §9 of the protocol requires;
- an optional agreement trial.

It is not the Phase 4B audit sample, and verifier accuracy is never claimed from verifier decisions alone. The Phase 4B audit sampling plan is recorded in `docs/llm_evaluation_protocol.md` §9 and §9.1, before Phase 4B.

## 7. Reproduce

```bash
uv run python -m phase4.eval.structured_tests_v2     # offline: mock Ollama + leakage scan (0 model calls)
uv run python -m phase4.eval.pilot_v2 plan            # selection rule -> pilot_plan.json (0 model calls)
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve &   # Colima stopped
uv run python -m phase4.eval.pilot_v2 run             # resumable; raw per call in results/phase4a_v2/raw/
uv run python -m phase4.eval.pilot_v2 analyze         # dev_comparison / claim_analysis / control_abstention / audit
```

## Observed results

See `docs/phase4a_v2_validation.md` (written after the pilot).
