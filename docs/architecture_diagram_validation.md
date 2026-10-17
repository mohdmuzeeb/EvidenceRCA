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

The figure was simplified on 2026-10-16 at the user's request: shorter labels and fewer boxes (§3). The map covers what the figure shows now.

| Figure element | Source |
|---|---|
| **A** testbed: API gateway, order, inventory, payment, PostgreSQL | `testbed/app/*.py`, `compose.yaml` |
| Testbed arrows: gateway→order, gateway→inventory, order→inventory, order→payment, order→PostgreSQL, inventory→PostgreSQL | `gateway.py:12-13`, `order.py:15-16`. Only order and inventory get `DATABASE_URL` (`compose.yaml` `x-db-env`); payment has no DB |
| **B** OpenTelemetry: traces · metrics · logs | `testbed/app/telemetry.py` (logs carry trace/span IDs, `:63-64`), `testbed/otel-collector.yaml`. The checksummed raw bundle is read only via `evidencerca/rca_input.py` |
| Normalization: typed records + ev-IDs | `evidence.py:232` `normalize`, `:61` `content_id` (`ev-` + 20 hex, content hash) |
| Evidence view: typed records | `evidence.py:244` `View`. One case under C0 or one of 11 perturbations (`evaluation/views.py` `SPECS`) |
| Shared alert detector → analysis window | `core.py:77` `detect_alert` (gateway SLO), `:103` `make_windows` (±60 s analysis window, plus a 90 s baseline) |
| Fault labels: evaluation only | `data/ground_truth/`; agent-side loading refuses it (`rca_input.py:50`). The harness uses it for scoring and view building (§1, row 8) |
| **E0** Anomaly signals: per service + onset | `signals.py:188` `evaluate_all` (median + 3·scaled MAD; onset) |
| Observed call graph: *not causal* | `core.py:110` `observed_graph` (invocation edges from spans) |
| Constrained scoring: callee anomalous first ⇒ caller is a symptom | `evidencerca_e0.py:48` `rank`. The callee's onset must be ≤ the caller's + 10 s (`:19`). The caller's latency, error and log signals are then dropped; intrinsic signals keep full weight and pool signals half |
| Ranked root candidates | `diagnosis.py` `finish`; the fault type comes from `infer_fault_type` (`:84`, not drawn) |
| Evidence retrieval: cited ev-IDs | `evidencerca_e0.py:108` `_cite`, `:114` `make_claims` (template claims), `:177` `propagation_path` |
| E0 → E1 "E0 ranking + evidence (E1 only)" | `conditions.py:58-66` (`ranking = e0`, `first_ids` lead E1's evidence pack, `evidence_pack.py:91`) |
| **E1** LLM explanation (qwen3:4b): writes claims; ranking stays E0's | `llm_client.py:25`; `conditions.py:85` (`diag[k] = ranking[k]`); the LLM's own pick is only logged |
| E2 (optional): canonical claims | `conditions_e2.py:19`, `ownership.py:146`. Original claims are kept; canonical claims are verified separately |
| **B4** LLM-only diagnosis (same model): own ranking, fault type, claims | `conditions.py:75-81`, with a method-neutral evidence pack (`evidence_pack.build(view, win, None)`) |
| B4-G (optional): no alert ⇒ ABSTAIN, no LLM call | `conditions_e2.py:48` `run_b4g` |
| **Evidence verifier**: deterministic, independent | `evidencerca/verifier.py`. It imports no `evidencerca.*` module (test-enforced) and re-reads the view itself, including its own call edges (`:127`) |
| V0 schema + unit · V1 evidence ID exists · V2 case + time window · V3 emitter / callee · V4 value + unit · V5 above baseline | `verifier.py:292-333` (spec §3). V4 allows a 5 % relative tolerance; V5 also covers onset order for `onset_before` |
| V6 observed call edge | `verifier.py:350` (propagation edges) |
| *never changes a ranking* | `verify(diag, view)` only returns verdicts; nothing writes them back to `ranked_candidates` |
| Accepted / rejected claims: *not audited truth* | `conditions.py` `_claim_sets`. All claims are kept and rejected claims are never rewritten (spec §3). The human audit is pending (`audit/`) |
| Abstention (optional): E0 only, ABSTAIN if S < τ | `abstention.py:73` `score`. S averages sufficiency, agreement, margin and verified coverage, hence the arrows from E0's ranking and from the verifier. τ was chosen on calibration seeds |

**Arrows.** Every solid arrow is a data dependency in the code above. The dashed ones are E2 and B4-G, both derived after the study from stored Phase 4B outputs, with no new calls. Box styles: blue fill = deterministic, grey fill = LLM call, thick blue border = output.

## 3. Simplifications

**Omitted from the simplified figure** (still true in the code):
- the evidence-pack boxes (≤36 records plus a digest; E1's pack starts with E0's records);
- the fault-type rule;
- the baseline window;
- the correlation IDs and checksummed raw bundle;
- the perturbation list;
- the detail of S's four components;
- the fact that all claims are kept and none rewritten.

**Never drawn:**
- **Call edges.** The testbed edges are the application's designed HTTP/SQL calls. E0's graph is re-observed per view and can lose edges (for example under MT, where traces are missing).
- **LLM plumbing.** The aliasing of ev-IDs as E01… (`prompts.py:56`) and the parse/schema step are folded into "LLM".
- **When E1 runs.** E1 calls the LLM only when E0 has a positive-score candidate, and Phase 4B used a median-time fallback window when the detector did not alert (§1, row 2).
- **Other methods.** Baselines B1–B3, the E0-A-graph ablation and the p4a-v2 dev pilot are not shown.

## 4. Visual checks (performed on the simplified figure)
- **Size:** the canvas is 517 × 231 pt (7.18 × 3.21 in, aspect 2.24). Scaled to `\textwidth` (≈ 7.16 in), it prints at about 99.7 %.
- **Fonts:** most text is 6.6 pt and group titles are 7.0 pt bold. The legend and the dashed optional boxes use 5.8–5.9 pt.
- **Rendering review** at 7 in wide (150 dpi):
  - no text is clipped and no boxes overlap;
  - every arrow is horizontal or vertical except the testbed's call edges, and no arrows cross;
  - the E0 → E1 hand-off arrow clears the E1 title.
- **PDF fonts:**
  - all fonts are embedded as subsets: Helvetica, Helvetica-Bold and Helvetica-Oblique;
  - Hiragino Sans and Lucida Grande are also embedded, as cairo's fallback for ⇒, τ and →.
- **Content:**
  - V0–V6 match `verifier.py`;
  - B4 (bottom lane, "no E0 input") is separate from E1 (middle lane, blue hand-off from E0);
  - there is no arrow from the verifier into a ranking.
- **Determinism (corrected).** The first version of this report said the PDF carried no creation date. That was wrong: cairo writes one inside a compressed object stream, which my uncompressed search missed. The build now sets `SOURCE_DATE_EPOCH`, and six rebuilds at least 1 s apart produced byte-identical PDF, SVG, PNG and DOT files.
- **Not done:** LaTeX is not installed, so the figure was **not** tested in a compiled manuscript, and the six-page fit is unverified. It takes about 3.2 in of height across both columns.

## 5. Suggested LaTeX (IEEEtran)

```latex
\begin{figure*}[t]
  \centering
  \includegraphics[width=\textwidth]{figures/evidencerca_architecture.pdf}
  \caption{EvidenceRCA architecture. Telemetry from the instrumented testbed is normalized into typed
  evidence records with content-hash IDs. A shared gateway-SLO detector fixes the analysis window. E0 ranks root-service candidates deterministically: per-service anomaly signals are scored under
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
