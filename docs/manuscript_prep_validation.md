# Manuscript preparation: what was done and how it was checked

**Scope (approved 2026-10-15):**
- the human-audit workflow for the existing 240-item package;
- annotator instructions;
- IEEE-ready tables and figures from recorded experiments;
- a manuscript outline.

No experiment was run, no model was called, no algorithm or result file was changed, no human label was created, and the audit key was not unsealed. The paper is not drafted or submitted.

## 1. Preservation

| Check | Result |
|---|---|
| Freeze manifest (`912085c`) | `verify_frozen()` passes (`tests/test_manuscript_prep.py`) |
| All 320 files tracked at the end of Phase 4B (`12af836`) | hash-locked in `results/manuscript_prep/preserved_before_manuscript_prep.json` and byte-identical. This covers frozen code, Phase 4A–4B code, all results, docs and tests. This stage only adds files, which is why the packet ignore rule lives in `audit/.gitignore` rather than the tracked root `.gitignore` |
| Earlier hash locks (Phase 4A–4B) | still pass in the full suite |
| Audit package | the CSV and key checksums equal `audit_manifest.json`. No submissions, reference labels or unsealed analysis exist (asserted by a test) |

**Line endings (pre-existing; disclosed, not changed).** Three CSVs were committed before `.gitattributes` existed:
- `results/phase4a_v2/audit/audit_sheet_dev_pilot.csv`;
- `results/phase4a_v3/audit_dry_run/sheet_A.csv` and `sheet_B.csv`.

Under the global `core.autocrlf=input`, their committed blobs have LF line endings, while the working files keep CRLF. The lock hashes the working files and records the LF blob hashes separately. Line endings are the only difference: this was asserted. The Phase 4B audit CSV is unaffected (it was fixed in `12af836`).

## 2. Audit workflow (`audit/`)

| File | Purpose |
|---|---|
| `audit/README.md` | Study-lead procedure: roles, worked examples, packets, submission checks, agreement, adjudication, label locking, unsealing, the single-annotator fallback, and what must never happen |
| `audit/ANNOTATOR_GUIDE.md` | Self-contained guide for annotators: rubric, fields, ownership rule, submission, adjudication meeting |
| `audit/audit_tool.py` | Commands: `check-package`, `worked-examples`, `packets`, `validate [--register]`, `agreement`, `finalize`, `unseal` |
| `audit/worked_examples.csv` | 10 **dev** items (2 per predicate group, seed 20261103) with **empty** label and explanation fields. The study lead fills them in. Claude did not label them |

**Safeguards built into the tool:**
- **Packets are blocked** until all 10 worked examples are labelled and explained. Packets never contain the key, the verdicts or the ground truth (tested).
- **Submissions** are validated, stored together with the original file, checksummed and made read-only. A registered submission cannot be replaced.
- **Agreement statistics** come from the pre-adjudication labels:
  - Cohen's κ, unweighted over 4 labels;
  - linear-weighted κ over the 3 ordinal labels;
  - Gwet's AC1;
  - relevance κ;
  - item-bootstrap CIs.
- **The adjudication sheet** lists only disagreements and shows no key information.
- **Reference labels** are written once. **`unseal`** refuses to run unless they are committed and unchanged. It is the only step that reads the key.
- **Analysis definitions** (strict and lenient precision, verified-claim error rate, population-weighted recall and hallucination rate, verifier–human κ, cluster-bootstrap CIs) follow protocol §9. They are fixed in code before any label exists.

**Tests.** `tests/test_audit_workflow.py` (11 tests) runs on a synthetic 12-item package in a temporary directory. It never reads the real key, and none of its synthetic labels are written to the repository.

**Disclosure.** While building the tool, Claude read the sealed key's field layout and incidentally printed some of its rows. Claude is not an annotator and produced no labels. The study lead also has access to the key and therefore must not annotate.

## 3. Tables and figures (`paper/`)

**Generation.** `uv run python -m paper.build_artifacts` reads only recorded result files. They are listed with their sha256 in `paper/artifact_manifest.json`:
- `results/final/test_results.json` and `test_trials.jsonl.gz`;
- `results/phase4b/condition_comparison.json`, `claim_metrics.json`, `runtime_summary.json` and `llm_trials.jsonl.gz`.

**Derived quantities** are recomputed with the frozen helpers (`evaluation/run_final.py: wilson, rc`) and asserted against the recorded values:
- Per-fault-type Phase 4B counts are aggregated from the stored per-view trials, and their totals equal the recorded 74/235 and 148/235 (top-1) and 48 and 134 (fault type).
- B4's wrong-service count equals the recorded 160.
- The risk–coverage curves come from stored frozen trials. Their AURCs reproduce the recorded 0.2135, 0.1603 and 0.1744 exactly.
- The rejection breakdowns sum to the recorded rejected counts.
- The E2 dispositions equal the recorded counts. 10 observed claims are not applicable (relational `onset_before`), plus the 3 inferred claims.

**Outputs:**
- 13 LaTeX tables (`paper/tables/*.tex`, booktabs) with a Markdown preview;
- 5 figures (SVG source, plus PDF for LaTeX and PNG preview);
- `paper/numbers.tex` macros.

**Tiers.** Every table caption begins with its tier, "Confirmatory (frozen protocol …)" or "Exploratory (Phase 4B, post-study …)" (tested).

**Formatting:**
- Rounding is half-up from the recorded decimals, matching the frozen reports (S AURC 0.214).
- Negative interval bounds are kept (for example R40 E0 vs B2: −0.06 [−0.21, 0.08]).
- Denominators appear in every count cell or header.

**Determinism.** A rebuild produces byte-identical SVG, TeX and Markdown outputs (tested). With `SOURCE_DATE_EPOCH` fixed, the PDFs were byte-identical across two runs.

**Tooling limits:**
- matplotlib is not installed, and nothing was downloaded. The figures are drawn by `paper/svgfig.py` (pure Python SVG) and converted with the local `rsvg-convert`.
- No LaTeX installation is available, so the `.tex` tables were **not compiled** here. Check them in the IEEE template.

## 4. Manuscript outline

`docs/manuscript_outline.md` covers:
- title options, abstract structure, section-by-section content, and figure/table placement;
- wording rules for each tier: claims that may be made, and claims that must not;
- limitations, and the placeholders marked `[AUDIT PENDING]`.

## 5. Status
- **Human audit: PENDING.** Next action, by the study lead: label the 10 worked examples, recruit two independent annotators, then follow `audit/README.md`.
- **Verifier accuracy:** not claimed anywhere.
- **Paper:** not drafted, not submitted.
