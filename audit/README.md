# Phase 4B human claim audit: study-lead workflow

**Status: PENDING.** No human labels exist and the key is sealed.

**Protocol.** `docs/llm_evaluation_protocol.md` §9 and §9.1.

**Package.** `results/phase4b/audit_blinded.csv`, `audit_key_sealed.json` and `audit_manifest.json`. Generated in Phase 4B (seed 20261101) and never modified.

All commands run from the repository root with `uv run python audit/audit_tool.py <command>`. The tool never creates a label. It reads the sealed key in exactly one step, `unseal`, and that step refuses to run until the adjudicated reference labels are committed to git.

## Roles

| Role | Who | Must not |
|---|---|---|
| Study lead | runs the tool, writes the worked examples, facilitates adjudication | annotate the 240 items; reveal conditions, verdicts or ground truth before `unseal` |
| Annotator A, Annotator B | label all 240 items independently | open `results/phase4b/`, the repository or each other's sheet; discuss items before both have submitted |
| Third annotator (optional) | decides items A and B cannot agree on | see the key |

**Pseudonyms.** Annotators are recorded by pseudonymous ids (A, B, C) only. Keep the mapping to real people outside the repository.

**Blinding condition.** Annotators must not have worked on the system's outputs.
- The study lead has access to the key and the system's outputs, so cannot serve as an annotator. Neither can Claude.
- Claude generated the package and wrote this tool. It has seen the key's field layout while building it, and it produces no labels.

## Steps

### 0. Check the package
```
uv run python audit/audit_tool.py check-package
```
This confirms the blinded sheet and the key match their manifest checksums, and that the sheet holds no labels.

### 1. Write the 10 worked examples (study lead, before recruiting starts)
- `audit/worked_examples.csv` already holds 10 **dev** items with empty label fields. They come from the dev dry-run sheet `results/phase4a_v2/audit/`, 2 per predicate group, seed 20261103, and never from the Phase 4B sample.
- For each item, fill in:
  - `support_label`, `partial_reason`, `evidence_relevance`;
  - a one- to three-sentence `explanation` that applies the rubric;
  - your initials in `labelled_by`.
- **Judge from the shown evidence only.**
  - Do not open the dev key (`audit_key_dev_pilot.SEALED.json`), and do not copy verifier verdicts.
  - Aim to show each label at least once, and include one `CLIENT`-span ownership case.
  - If a selected item is unsuitable, for example truly ambiguous, keep it and explain why it is hard. That is useful for annotators.
- Commit the completed file.

### 2. Build and send the packets
```
uv run python audit/audit_tool.py packets
```
- The command refuses to run until the 10 worked examples are complete.
- It writes `audit/packets/annotator_A/` and `annotator_B/`, each containing:
  - `items.html`, a read-only browser view;
  - `labels_<ID>.csv` to fill in;
  - the blinded CSV, `ANNOTATOR_GUIDE.md`, `worked_examples.csv` and a checksum manifest.
- The key is never copied. The packets are generated files and are gitignored.
- Send each annotator only their own folder, zipped.
  - Agree on a deadline. Annotation takes about 6–8 h.
  - Ask annotators to log their hours and the dates they worked.

### 3. Receive and register submissions
```
uv run python audit/audit_tool.py validate path/to/labels_A.csv --annotator A              # check only
uv run python audit/audit_tool.py validate path/to/labels_A.csv --annotator A --register   # store
```
- **Validation checks that:**
  - every audit id is present exactly once;
  - every value is from the allowed lists;
  - `partial_reason` is filled exactly when the label is `partially_supported`;
  - `evidence_relevance` is filled unless the label is `not_assessable`;
  - if an annotator returns the full sheet instead, its item content is unchanged.
- **Problems** are listed by item id. Return the list to the annotator, who corrects only those rows.
- **Registering** stores a normalized copy, `audit/submissions/labels_<ID>.csv`, together with the original file. It records both checksums in `submission_log.json` and makes the files read-only. A registered submission can't be replaced.
- **Commit the registered submissions immediately.** These are the pre-adjudication labels, and they are never edited afterwards.

### 4. Agreement and the adjudication sheet
```
uv run python audit/audit_tool.py agreement
```
- **Statistics** go to `audit/results/agreement_A_vs_B.json`. They are computed from the pre-adjudication labels only:

  | Field | Statistic |
  |---|---|
  | `support_label` | percent agreement; Cohen's κ, unweighted over 4 labels; linear-weighted κ over unsupported < partially_supported < supported (items with `not_assessable` from either annotator are excluded and counted); Gwet's AC1 |
  | `evidence_relevance` | unweighted κ |

  Item-bootstrap 95 % CIs are included.
- **Adjudication sheet:** `audit/adjudication/adjudication_sheet.csv`. It lists only the items whose support label differs, whose relevance differs, or whose partial reason differs when both annotators chose `partially_supported`.
  - Both annotators' labels and notes appear side by side.
  - Fields the annotators agree on are pre-filled.
  - The sheet shows no condition, verdict or ground truth.
- **Commit** `agreement_A_vs_B.json` and the empty adjudication sheet.

### 5. Adjudicate
Hold one meeting, or several, with A and B. Show each listed item in `items.html` together with the sheet. For each row:

| Column | What to enter |
|---|---|
| `adjudicated_support_label`, `adjudicated_partial_reason`, `adjudicated_evidence_relevance` | the agreed values |
| `resolution` | `discussion`; or `third_annotator` (the third annotator, blinded, decided); or `unresolved` (label fields may stay empty) |
| `adjudication_notes` | one line on the reason |

Commit the completed sheet.

### 6. Fix the reference labels
```
uv run python audit/audit_tool.py finalize
git add audit/results/reference_labels.csv audit/results/reference_manifest.json && git commit -m "Audit reference labels (pre-unseal)"
```
- Agreed items take the common label, and adjudicated items take the decided label. Unresolved items are kept as `unresolved` and reported.
- The reference labels can't be rewritten: `finalize` refuses if they exist.

### 7. Unseal and analyze
```
uv run python audit/audit_tool.py unseal
```
- **Preconditions.** `unseal` refuses unless:
  - `reference_labels.csv` is committed and unchanged;
  - it covers all 240 items;
  - the key still matches its manifest checksum.
- **Output.** `audit/results/audit_analysis.json` gives, per condition (E0, E1, B4), strict and lenient (partial counted as unsupported or as supported):
  - verified-claim precision, and the verified-claim error rate;
  - agreement among the verifier's rejections;
  - verifier recall and the hallucination rate, weighted back to the claim population by stratum;
  - κ between verifier and human labels;
  - counts by predicate group, relevance counts, and human-supported claims about the true root.
- **Exclusions and CIs.** `not_assessable` and `unresolved` items are excluded from the rates and counted. CIs use a cluster bootstrap over base cases (10,000 resamples, seed 7).
- **Caveat.** The E0 rejected stratum is 39/40 `onset_before`, so E0's rejection statistics describe that predicate only. The output records this.
- **Commit** the analysis. Only now may papers quote verifier precision or recall. The quoted figures are the **adjudicated-label** estimates, with their CIs.

## Single-annotator fallback (report it if used)
If only one annotator (A) is available:
1. Run steps 0–3 for A.
2. Two weeks or more after A submits, run `packets --relabel A`.
   - This is a seeded 20 % subset (48 items, seed 20261104) in a different order.
   - A labels it without access to the first pass.
3. Register it as annotator `A-relabel`.
4. Run `agreement --first A --second A-relabel --intra` to get the intra-rater statistics. No adjudication sheet is produced.
5. Run `finalize --single A`, commit, then `unseal`.

**Reporting.** The paper must say that the reference rests on a single annotator, and it must report the intra-rater κ.

## What must never happen
- No label is typed by the study lead, Claude, or any script. Verifier verdicts are never copied in as labels.
- No annotator sees a condition, verdict or ground truth before `unseal`.
- Registered submissions, the adjudication decisions and the reference labels are never edited after they are committed. Corrections are new, documented files.
- The audit sample is never resampled or extended after labels are seen.
