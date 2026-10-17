# Architecture diagram: validation against the implementation

**Files** (`paper/figures/`):
- `evidencerca_architecture.pdf` (for LaTeX), `.svg`, `.png` (300 dpi preview);
- `evidencerca_architecture_source.dot`: Graphviz source; all positions are pinned;
- `evidencerca_architecture_build.py`: writes the `.dot` and renders it with the local Graphviz 13.1.2 (`neato -n2`, cairo).

Rebuild: `uv run python paper/figures/evidencerca_architecture_build.py`.

**Scope.** Documentation only. No implementation code, algorithm, result, freeze manifest, existing figure or manuscript file was changed, and no experiment or LLM call was run. Nothing was downloaded.

## 1. Discrepancies between the request or outline and the code (the figure follows the code)

| # | Request / outline | Implementation | Figure |
|---|---|---|---|
| 1 | V3 "emitting-service ownership" | V3 is *association*: each cited record's modality, measurement **and service relation** must fit the predicate. The subject is the emitter for SERVER spans, metrics and logs, and the **callee** for CLIENT spans (`verifier.py:316`; spec §2–3) | "V3 emitter/callee ownership" |
| 2 | B4 diagnoses "from telemetry and evidence" | B4 also receives the **shared alert detector's analysis window**. If the detector does not alert, Phase 4B uses a label-free median-time window (`phase4/eval/phase4b.py:77` `window_for`). The window is identical for B4 and E1, and B4 never sees E0's ranking | Both LLM lanes take input from the shared view-and-detector column; there is no E0 → B4 path |
| 3 | The alert detector as part of E0 | `detect_alert` (`evidencerca/core.py:77`) is **shared**: E0, B4, E1 and the B4-G gate all use it | Drawn in the shared column, not inside E0 |
| 4 | "Anomaly detection" as one step | Two different things: the gateway-SLO **alert detector**, which only opens the window, and the **per-service anomaly signals** (`signals.py:188`) that E0 scores | Shown separately |
| 5 | "Provenance" of evidence | Evidence records carry **no pointer into raw files** (`evidence.py` docstring). Provenance here means the record's own metadata (emitter, span kind, callee, family, unit), which V3 and E2's ownership step read (`ownership.py`). Raw bundles are immutable and checksummed (`rca_input.py`) | "typed evidence records … emitter · kind · callee · t"; "checksummed raw bundle" |
| 6 | Verifier V0–V6 for every claim | Observed claims are accepted only if V0–V5 all pass. V6 applies to **propagation edges**. Inferred claims are checked on V0–V3 only and are never "verified" (`verifier.py:318`). Phase 4B text says "accepted by V0–V5" for claims | V6 listed as "edge in observed calls" |
| 7 | Abstention as a general output | E0's composite score S (`abstention.py:73`) is thresholded **for E0 only**. For the LLM conditions it is computed descriptively but not thresholded (`conditions.py` docstring). S uses ranking margin, signals and verified-claim coverage | "Abstention (optional): E0 only"; the arrows come from E0's ranking and from the verifier (coverage) |
| 8 | The ground truth stays outside the pipeline | True for every method: `rca_input.load_case` raises `GroundTruthAccessError` (`rca_input.py:50`). The **evaluation harness** does read labels for two things: scoring, and **building perturbed views** (`evaluation/views.py:40`; for example, the CF decoy is chosen from non-root services). Methods see only the resulting view | Dashed box "Fault-injection labels: sealed from the pipeline; scoring + view building only", with no arrow into the pipeline |
| 9 | The outline (`manuscript_outline.md` §III-B) says the schematic is "to be drawn by hand; not generated" | This figure is generated, with every position set by hand | Update the outline's checklist item when you accept the figure (not edited here) |

## 2. Component → source map

| Figure element | Source |
|---|---|
| **A** testbed: API gateway, order, inventory, payment, PostgreSQL | `testbed/app/*.py`, `compose.yaml` |
| Testbed arrows: gateway→order, gateway→inventory, order→inventory, order→payment, order→PostgreSQL, inventory→PostgreSQL | `gateway.py:12-13`, `order.py:15-16`; DB access via `DATABASE_URL` for order and inventory only (`compose.yaml` `x-db-env`). Payment has no DB |
| "faults injected here" | `harness/faults.py` (injection acts on the running testbed) |
| **B** OpenTelemetry traces · metrics · logs | `testbed/app/telemetry.py`, `testbed/otel-collector.yaml` (OTLP, docker_stats, fluentd for Postgres) |
| "logs carry trace/span IDs" (correlation identifiers) | `telemetry.py:63-64` |
| "checksummed raw bundle" | `evidencerca/rca_input.py` (manifest SHA-256, allowed files only) |
| Normalization; ID = content hash | `evidence.py:232` `normalize`, `:61` `content_id` (`ev-` + 20 hex) |
| Evidence view: one case, C0 or one of 11 perturbations | `evidence.py:244` `View`; `evaluation/views.py` `SPECS` (12 view types) |
| Shared alert detector → analysis + baseline windows | `core.py:77` `detect_alert` (gateway SLO, 3 consecutive 5 s buckets), `:103` `make_windows` (±60 s analysis, 90 s baseline) |
| **C** Anomaly signals: robust baseline (MAD), onset | `signals.py` (median + 3·scaled MAD; onset = first of ≥2 exceeding buckets) |
| Observed call graph: *invocation edges, not a causal graph* | `core.py:110` `observed_graph` (CLIENT→SERVER parent/child pairs, CLIENT peer) |
| Constrained scoring (callee anomalous no later than caller + 10 s ⇒ caller's latency/error/log signals are symptoms) | `evidencerca_e0.py:48` `rank`, `:19` `ONSET_TOLERANCE_NS`; intrinsic signals keep full weight, pool signals half |
| Ranked root candidates + fault-type rule | `diagnosis.py` `finish`; `evidencerca_e0.py:84` `infer_fault_type` |
| **D** Evidence retrieval → template claims | `evidencerca_e0.py:108` `_cite`, `:114` `make_claims`, `:177` `propagation_path` |
| E0 → E1 hand-off "E0 ranking + retrieved ev-IDs (to E1 only)" | `conditions.py:58-66` (`ranking = e0`, `first_ids`) |
| **E1** Evidence pack: E0's records first, neutral fill to ≤36, + digest and call graph | `evidence_pack.py:25`, `:91` `build(view, win, first_ids)` |
| E1 LLM: qwen3:4b, local; ranking stays E0's; the LLM's pick is logged | `llm_client.py:25`; `conditions.py:85` (`diag[k] = ranking[k]`), `agrees_with_e0_top1` |
| E2 (optional): provenance → canonical claims | `conditions_e2.py:19` `e2_from_e1`; `ownership.py:146` `canonicalize`. Original claims are kept, and canonical claims are verified separately |
| **B4** Evidence pack, method-neutral | `evidence_pack.build(view, win, None)` |
| B4 LLM-only diagnosis: own ranking, fault type, claims | `conditions.py:75-81` |
| B4-G (optional): the detector gates B4 | `conditions_e2.py:48` `run_b4g` |
| **F** verifier: deterministic, independent, re-reads the view | `evidencerca/verifier.py`; it imports no `evidencerca.*` module (test-enforced; spec §1). Its call edges are re-derived (`verifier.py:127` `call_edges`), not taken from E0's graph |
| V0–V5 | `verifier.py:292-333` (spec §3) |
| V6 | `verifier.py:350` in `verify` (propagation edges; spec §3) |
| "checks claims only; never changes a ranking" | `verify(diag, view)` returns verdicts. No caller writes them back to `ranked_candidates` (`conditions.py`, `evidencerca_e0.py`) |
| **G** Accepted / rejected claims; all kept, none rewritten; *accepted ≠ audited truth* | `conditions.py` `_claim_sets` (pre-filter, post-filter, rejected ids); spec §3–4; the human audit is pending (`audit/`) |
| Abstention (optional, E0 only) | `abstention.py:73` `score`; τ was chosen on calibration seeds (protocol) |

**Arrows.** Every solid arrow is a data dependency in the code above. Dashed arrows are E2 and B4-G, both derived after the study from stored outputs (Phase 4B), with no new calls. The colors encode the component type: blue fill = deterministic; grey fill = LLM call; thick blue border = output.

## 3. Simplifications
- **Call edges.** The testbed edges are the application's designed HTTP/SQL calls. E0's graph is re-observed per view from spans and can lose edges (for example under MT, where traces are missing).
- **Evidence pack contents.** The pack's digest of descriptive statistics, the aliasing of ev-IDs as E01… for the prompt (`prompts.py:56`), and the parse/schema step are summarized as "Evidence pack" and "LLM".
- **When E1 runs.** E1 calls the LLM only when E0 has a positive-score candidate. B4 and E1 also used the median-time fallback window in Phase 4B (§1, row 2). Neither is drawn.
- **Other methods.** Baselines B1–B3 and the E0-A-graph ablation are not drawn; the latter is E0 without the graph constraint.
- **Prompt versions.** The p4a-v2 typed gate (`conditions_v2.py`) was a dev pilot, not used in Phase 4B, and is not drawn.

## 4. Visual checks (performed)
- **Size and fonts:**
  - The canvas is 518 × 247 pt (7.19 × 3.43 in, aspect 2.10). Scaled to `\textwidth` (≈ 7.16 in), it is printed at about 99.6 %.
  - The smallest text is 5.8 pt and most body text is 6.2 pt; group titles are 6.6 pt bold.
- **Rendering review.** I viewed renders at 7 in wide (150 dpi) and at 6× zoom:
  - no text is clipped and no boxes overlap;
  - every arrow is horizontal or vertical, except the testbed's call edges, and no arrows cross.
  - The legend's dashed sample is drawn as three short segments; a single 12 pt dashed line rendered as one dash.
- **PDF fonts.**
  - All fonts are embedded as subsets: 12 `FontFile` entries for Helvetica, Helvetica-Bold and Helvetica-Oblique.
  - Hiragino Sans and Lucida Grande are also embedded, as cairo's fallback for ⇒, ≠, τ, ≤ and →.
  - If the publisher's PDF check rejects mixed fonts, replace those glyphs with ASCII in the build script.
- **Content checks:**
  - V0–V6 are checked against `verifier.py` and the spec (§1, row 1).
  - B4 (grey, lower lane, "no E0 input") is visibly separate from E1 (middle lane, blue hand-off arrow from E0).
  - There is no arrow from the verifier into E0's ranking.
- **Determinism.** Two consecutive builds produced byte-identical PDF, SVG, PNG and DOT files. The PDF carries no creation date.
- **Not done:** LaTeX is not installed (`pdflatex` and `tectonic` are absent), so the figure was **not** tested in a compiled manuscript, and the six-page fit is unverified. The figure takes about 3.4 in of height across both columns, roughly 35 % of one page.

## 5. Suggested LaTeX (IEEEtran)

```latex
\begin{figure*}[t]
  \centering
  \includegraphics[width=\textwidth]{figures/evidencerca_architecture.pdf}
  \caption{EvidenceRCA architecture. Telemetry from the instrumented testbed is normalized into typed
  evidence records with content-hash IDs. A shared gateway-SLO detector fixes the analysis and baseline
  windows. E0 ranks root-service candidates deterministically: per-service anomaly signals are scored under
  the observed caller--callee graph and onset order (invocation edges, not a causal graph), and the cited
  evidence is retrieved. In the exploratory LLM branches, E1's model explains E0's ranking without changing it,
  while B4's model diagnoses alone and never sees E0's output. A deterministic verifier (V0--V6) independently
  re-reads the view and accepts or rejects each claim; it never alters a ranking, and acceptance is not
  audited factual correctness. Dashed: optional post-study branches (E2, B4-G).
  Fault-injection labels are used only for evaluation.}
  \label{fig:architecture}
\end{figure*}
```

A shorter caption, if space is tight: *"EvidenceRCA architecture. E0 localizes deterministically under the observed call graph; LLM branches B4 (alone) and E1 (explaining E0's ranking) are exploratory; the independent verifier V0–V6 accepts or rejects claims but never changes a ranking. Dashed: optional branches."*
