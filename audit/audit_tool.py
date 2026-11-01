"""Phase 4B human-audit workflow: packets, submission checks, agreement, adjudication and (only at the end) unsealing.

Protocol: docs/llm_evaluation_protocol.md section 9 and 9.1; study-lead steps: audit/README.md.

This tool never creates a human label. Every label it reads comes from a file a person filled in. It reads the
sealed key only in `unseal`, and `unseal` refuses unless the adjudicated reference labels are already committed to
git and unchanged, so the labels are fixed before anyone sees conditions, verdicts or ground truth.

Usage (from the repository root):
    uv run python audit/audit_tool.py check-package
    uv run python audit/audit_tool.py worked-examples            # 10 dev items for the study lead to label
    uv run python audit/audit_tool.py packets                    # after the worked examples are complete
    uv run python audit/audit_tool.py validate FILE --annotator A [--register]
    uv run python audit/audit_tool.py agreement                  # A vs B; writes the adjudication sheet
    uv run python audit/audit_tool.py finalize                   # merges agreed + adjudicated labels
    uv run python audit/audit_tool.py unseal                     # only after reference labels are committed
Single-annotator fallback: `packets --relabel A` (seeded 20 % re-label sheet), then
`agreement --first A --second A-relabel --intra`, and `finalize --single A`.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
P4B = REPO / "results/phase4b"
BLINDED = P4B / "audit_blinded.csv"
KEY = P4B / "audit_key_sealed.json"
MANIFEST = P4B / "audit_manifest.json"
DEV_SHEET = REPO / "results/phase4a_v2/audit/audit_sheet_dev_pilot.csv"
AUDIT = REPO / "audit"
WORKED = AUDIT / "worked_examples.csv"
PACKETS = AUDIT / "packets"
SUBMISSIONS = AUDIT / "submissions"
ADJ = AUDIT / "adjudication"
OUT = AUDIT / "results"

SUPPORT = ("supported", "partially_supported", "unsupported", "not_assessable")
ORDINAL = {"unsupported": 0, "partially_supported": 1, "supported": 2}   # not_assessable is not on the scale
PARTIAL_REASONS = ("value", "unit", "window", "object", "subset_of_citations", "other")
RELEVANCE = ("relevant", "weakly_relevant", "irrelevant")
LABEL_FIELDS = ("support_label", "partial_reason", "evidence_relevance", "notes")
RESOLUTIONS = ("discussion", "third_annotator", "unresolved")
CONDITIONS = ("E0", "E1", "B4")

WORKED_SEED = 20261103   # dev worked-example selection; distinct from 20261015 (dev sheet) and 20261101 (4B sample)
RELABEL_SEED = 20261104  # single-annotator 20 % re-label subset
RELABEL_SHARE = 0.20
BOOT_B, BOOT_SEED = 10_000, 7   # as the frozen cluster bootstrap
PRED_GROUP = {"latency_above_baseline": "latency/cpu/pool", "cpu_above_baseline": "latency/cpu/pool",
              "pool_wait_above_baseline": "latency/cpu/pool", "error_rate_above_baseline": "errors/calls/timeouts/logs",
              "call_failed": "errors/calls/timeouts/logs", "timeout_observed": "errors/calls/timeouts/logs",
              "log_error_pattern": "errors/calls/timeouts/logs", "no_telemetry_from": "silence",
              "onset_before": "onset"}   # = phase4/eval/pilot_v2.py PRED_GROUP (copied, not imported, to keep this standalone)


class AuditError(Exception):
    pass


# ---------------------------------------------------------------- io helpers
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> list[dict]:
    """Read a CSV as saved by a spreadsheet program: tolerates a UTF-8 BOM and ; or tab delimiters."""
    text = path.read_bytes().decode("utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text, newline=""), dialect=dialect))
    return [{(k or "").strip(): (v or "").strip() if k in LABEL_FIELDS or k == "audit_id" else (v or "")
             for k, v in r.items()} for r in rows]


def write_rows(path: Path, fields: list[str], rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def blinded_items() -> list[dict]:
    with open(BLINDED, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def check_package() -> dict:
    man = json.loads(MANIFEST.read_text())
    if sha256(BLINDED) != man["csv_sha256"]:
        raise AuditError("audit_blinded.csv does not match its manifest checksum; do not distribute it")
    if KEY.exists() and sha256(KEY) != man["key_sha256"]:   # the key is withheld from the public repository
        raise AuditError("audit_key_sealed.json does not match its manifest checksum")
    items = blinded_items()
    ids = [r["audit_id"] for r in items]
    if len(ids) != man["n_items"] or len(set(ids)) != len(ids):
        raise AuditError("blinded sheet item count or ids are inconsistent with the manifest")
    if any(r[f] for r in items for f in ("support_label", "partial_reason", "evidence_relevance", "notes")):
        raise AuditError("the blinded sheet already contains labels; it must be distributed empty")
    return man


# ---------------------------------------------------------------- worked examples (dev only)
def _dev_group(claim: str) -> str:
    m = re.match(r"(OBSERVED|INFERRED): (\w+)", claim)
    if not m:
        return "other"
    return "inferred" if m.group(1) == "INFERRED" else PRED_GROUP.get(m.group(2), "other")


WORKED_FIELDS = ["example_id", "dev_audit_id", "predicate_group", "windows", "claim", "cited_records",
                 "context_baseline_same_family", "context_analysis_same_family", "observed_call_edges",
                 "support_label", "partial_reason", "evidence_relevance", "explanation", "labelled_by"]


def worked_examples(force=False):
    """Select 10 dev-pilot items (2 per predicate group, seeded) and write an EMPTY template for the study lead."""
    if WORKED.exists() and not force:
        raise AuditError(f"{WORKED.relative_to(REPO)} exists; it may already hold the study lead's labels")
    with open(DEV_SHEET, newline="", encoding="utf-8") as f:
        dev = list(csv.DictReader(f))
    rng = random.Random(WORKED_SEED)
    by_group: dict[str, list] = {}
    for r in dev:
        by_group.setdefault(_dev_group(r["claim"]), []).append(r)
    picked = []
    for g in ("latency/cpu/pool", "errors/calls/timeouts/logs", "silence", "onset", "inferred"):
        pool = sorted(by_group.get(g, []), key=lambda r: r["audit_id"])
        picked += [(g, r) for r in rng.sample(pool, min(2, len(pool)))]
    rest = sorted((r for r in dev if r not in [p for _, p in picked]), key=lambda r: r["audit_id"])
    while len(picked) < 10:
        r = rest.pop(rng.randrange(len(rest)))
        picked.append((_dev_group(r["claim"]), r))
    rows = [{"example_id": f"W{i:02d}", "dev_audit_id": r["audit_id"], "predicate_group": g,
             **{k: r[k] for k in ("windows", "claim", "cited_records", "context_baseline_same_family",
                                  "context_analysis_same_family", "observed_call_edges")}}
            for i, (g, r) in enumerate(picked, 1)]
    write_rows(WORKED, WORKED_FIELDS, rows)
    print(f"wrote {WORKED.relative_to(REPO)}: {len(rows)} dev items, labels EMPTY (the study lead fills them)")


def worked_examples_complete() -> list[str]:
    """Problems that block distribution; empty when the 10 examples are labelled and explained."""
    if not WORKED.exists():
        return ["worked_examples.csv does not exist (run `worked-examples`, then label it)"]
    problems = []
    rows = read_rows(WORKED)
    if len(rows) != 10:
        problems.append(f"expected 10 worked examples, found {len(rows)}")
    for r in rows:
        problems += [f"{r['example_id']}: {p}" for p in label_problems(r)]
        if not r.get("explanation", "").strip():
            problems.append(f"{r['example_id']}: explanation is empty")
        if not r.get("labelled_by", "").strip():
            problems.append(f"{r['example_id']}: labelled_by is empty")
    return problems


# ---------------------------------------------------------------- packets
def _render_item(r: dict, n: int) -> str:
    e = lambda s: html.escape(s or "")
    ctx = lambda title, body: (f"<details><summary>{title}</summary><pre>{e(body) or '(none)'}</pre></details>")
    return (f'<section id="{e(r["audit_id"])}"><h2>{n}. {e(r["audit_id"])}</h2>'
            f'<p class="win">{e(r["windows"])}</p><p class="claim">{e(r["claim"])}</p>'
            f'<h3>Cited records</h3><pre>{e(r["cited_records"])}</pre>'
            f'<h3>Observed call edges</h3><pre>{e(r["observed_call_edges"]) or "(none)"}</pre>'
            + ctx("Baseline context (same family, ≤ 30)", r["context_baseline_same_family"])
            + ctx("Analysis context (same family, ≤ 30)", r["context_analysis_same_family"]) + "</section>")


def render_html(items: list[dict], title: str, worked: list[dict] | None) -> str:
    css = ("body{font:15px/1.45 system-ui,sans-serif;max-width:980px;margin:0 auto;padding:16px;color:#111;background:#fff}"
           "pre{white-space:pre-wrap;font:12.5px/1.35 ui-monospace,Menlo,monospace;background:#f5f5f5;padding:8px;"
           "border-radius:4px}.claim{font:600 15px ui-monospace,Menlo,monospace;background:#eef3ff;padding:8px}"
           ".win{color:#444}section{border-top:2px solid #ccc;margin-top:28px}.ex{background:#fffbea;padding:8px}"
           "@media (prefers-color-scheme: dark){body{background:#151515;color:#eee}pre{background:#222}"
           ".claim{background:#1f2a44}.win{color:#bbb}.ex{background:#332d10}}")
    parts = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,"
             f"initial-scale=1'><title>{html.escape(title)}</title><style>{css}</style><h1>{html.escape(title)}</h1>",
             "<p>Read-only view of the items. Record your labels in the labels CSV of this packet "
             "(see ANNOTATOR_GUIDE.md). Item order is fixed; the same ids appear in the CSV.</p>"]
    if worked:
        parts.append("<h1>Worked examples (dev data, not part of the audit)</h1>")
        for w in worked:
            parts.append(_render_item({**w, "audit_id": w["example_id"]}, 0).replace("<h2>0. ", "<h2>")
                         + f'<p class="ex"><b>Label:</b> {html.escape(w["support_label"])}'
                         + (f' ({html.escape(w["partial_reason"])})' if w["partial_reason"] else "")
                         + f'; <b>relevance:</b> {html.escape(w["evidence_relevance"])}<br>'
                         + f'<b>Why:</b> {html.escape(w["explanation"])}</p>')
        parts.append("<h1>Audit items</h1>")
    parts += [_render_item(r, i) for i, r in enumerate(items, 1)]
    return "\n".join(parts) + "\n</html>\n"


def make_packets(annotators=("A", "B"), relabel: str | None = None):
    man = check_package()
    problems = worked_examples_complete()
    if problems:
        raise AuditError("worked examples are not complete; packets are not distributed without them:\n  "
                         + "\n  ".join(problems))
    items, worked = blinded_items(), read_rows(WORKED)
    worked_view = [{k: w.get(k, "") for k in WORKED_FIELDS if k != "labelled_by"} for w in worked]
    jobs = [(a, items) for a in annotators]
    if relabel:
        rng = random.Random(RELABEL_SEED)
        ids = set(rng.sample([r["audit_id"] for r in items], round(RELABEL_SHARE * len(items))))
        subset = [r for r in items if r["audit_id"] in ids]
        rng.shuffle(subset)   # a different order from the first pass
        jobs = [(f"{relabel}-relabel", subset)]
    for name, its in jobs:
        d = PACKETS / f"annotator_{name}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "items.html").write_text(render_html(its, f"EvidenceRCA claim audit: annotator {name}", worked_view),
                                      encoding="utf-8")
        write_rows(d / f"labels_{name}.csv", ["audit_id", *LABEL_FIELDS], [{"audit_id": r["audit_id"]} for r in its])
        shutil.copyfile(BLINDED, d / "audit_blinded.csv")
        shutil.copyfile(AUDIT / "ANNOTATOR_GUIDE.md", d / "ANNOTATOR_GUIDE.md")
        write_rows(d / "worked_examples.csv", [k for k in WORKED_FIELDS if k != "labelled_by"], worked_view)
        pm = {"annotator": name, "n_items": len(its), "item_ids_sha256": hashlib.sha256(
                  "\n".join(r["audit_id"] for r in its).encode()).hexdigest(),
              "source_csv_sha256": man["csv_sha256"], "worked_examples_sha256": sha256(WORKED),
              "relabel_seed": RELABEL_SEED if relabel else None,
              "files": {p.name: sha256(p) for p in sorted(d.iterdir())},
              "excluded_by_design": ["audit_key_sealed.json", "conditions", "verifier verdicts", "ground truth"]}
        (d / "packet_manifest.json").write_text(json.dumps(pm, indent=1) + "\n")
        print(f"packet {d.relative_to(REPO)}: {len(its)} items")


# ---------------------------------------------------------------- validation
def label_problems(r: dict) -> list[str]:
    s, pr, rel = r.get("support_label", ""), r.get("partial_reason", ""), r.get("evidence_relevance", "")
    out = []
    if s not in SUPPORT:
        out.append(f"support_label {s!r} is not one of {', '.join(SUPPORT)}")
    if s == "partially_supported" and pr not in PARTIAL_REASONS:
        out.append(f"partial_reason {pr!r} must be one of {', '.join(PARTIAL_REASONS)} for partially_supported")
    if s != "partially_supported" and pr:
        out.append("partial_reason is filled but support_label is not partially_supported")
    if rel not in RELEVANCE and not (s == "not_assessable" and rel == ""):
        out.append(f"evidence_relevance {rel!r} is not one of {', '.join(RELEVANCE)}")
    return out


def validate(path: Path, expected_ids: list[str] | None = None) -> list[dict]:
    """Return the submission's label rows in item order, or raise with every problem found."""
    rows = read_rows(path)
    if not rows:
        raise AuditError(f"{path}: no rows")
    cols = set(rows[0])
    if not {"audit_id", "support_label", "partial_reason", "evidence_relevance"} <= cols:
        raise AuditError(f"{path}: needs columns audit_id, support_label, partial_reason, evidence_relevance")
    items = {r["audit_id"]: r for r in blinded_items()}
    expected = expected_ids if expected_ids is not None else list(items)
    problems = []
    ids = [r["audit_id"] for r in rows]
    if len(set(ids)) != len(ids):
        problems.append("duplicate audit_id rows")
    if set(ids) != set(expected):
        miss, extra = sorted(set(expected) - set(ids)), sorted(set(ids) - set(expected))
        problems.append(f"item ids differ from the packet (missing {miss[:10]}, unexpected {extra[:10]})")
    content_cols = [c for c in ("windows", "claim", "cited_records", "context_baseline_same_family",
                                "context_analysis_same_family", "observed_call_edges") if c in cols]
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()
    for r in rows:
        problems += [f"{r['audit_id']}: {p}" for p in label_problems(r)]
        src = items.get(r["audit_id"])
        if src and any(norm(r[c]) != norm(src[c]) for c in content_cols):   # full-sheet submissions only
            problems.append(f"{r['audit_id']}: item content differs from the distributed sheet")
    if problems:
        raise AuditError(f"{path}: {len(problems)} problem(s)\n  " + "\n  ".join(problems[:60]))
    order = {a: i for i, a in enumerate(expected)}
    return sorted(({k: r.get(k, "") for k in ("audit_id", *LABEL_FIELDS)} for r in rows),
                  key=lambda r: order[r["audit_id"]])


def register(path: Path, annotator: str):
    expected = None
    if annotator.endswith("-relabel"):
        pm = json.loads((PACKETS / f"annotator_{annotator}" / "packet_manifest.json").read_text())
        expected = [r["audit_id"] for r in read_rows(PACKETS / f"annotator_{annotator}" / f"labels_{annotator}.csv")]
        assert hashlib.sha256("\n".join(expected).encode()).hexdigest() == pm["item_ids_sha256"]
    rows = validate(path, expected)
    dest = SUBMISSIONS / f"labels_{annotator}.csv"
    if dest.exists():
        raise AuditError(f"{dest.relative_to(REPO)} is already registered; pre-adjudication labels are never replaced")
    SUBMISSIONS.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, SUBMISSIONS / f"labels_{annotator}.original{path.suffix}")
    write_rows(dest, ["audit_id", *LABEL_FIELDS], rows)
    log_path = SUBMISSIONS / "submission_log.json"
    log = json.loads(log_path.read_text()) if log_path.exists() else {"submissions": []}
    log["submissions"].append({"annotator": annotator, "registered_at": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "n_items": len(rows), "normalized_sha256": sha256(dest),
        "original_file_sha256": sha256(path), "blinded_csv_sha256": sha256(BLINDED)})
    log_path.write_text(json.dumps(log, indent=1) + "\n")
    for p in (dest, SUBMISSIONS / f"labels_{annotator}.original{path.suffix}"):
        p.chmod(0o444)
    print(f"registered {dest.relative_to(REPO)} ({len(rows)} items); commit it before running `agreement`")


# ---------------------------------------------------------------- agreement statistics
def cohen_kappa(a: list, b: list, cats, weighted=False):
    n = len(a)
    if n == 0:
        return None
    idx = {c: i for i, c in enumerate(cats)}
    q = len(cats)
    w = (lambda i, j: 1 - abs(i - j) / (q - 1)) if weighted else (lambda i, j: 1.0 if i == j else 0.0)
    pa = [sum(x == c for x in a) / n for c in cats]
    pb = [sum(x == c for x in b) / n for c in cats]
    po = sum(w(idx[x], idx[y]) for x, y in zip(a, b)) / n
    pe = sum(w(i, j) * pa[i] * pb[j] for i in range(q) for j in range(q))
    return None if pe >= 1 else (po - pe) / (1 - pe)


def gwet_ac1(a: list, b: list, cats):
    n, q = len(a), len(cats)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pi = [(sum(x == c for x in a) + sum(y == c for y in b)) / (2 * n) for c in cats]
    pe = sum(p * (1 - p) for p in pi) / (q - 1)
    return (po - pe) / (1 - pe)


def _boot(pairs: list, stat, b=2000, seed=BOOT_SEED):
    """Item-level percentile bootstrap (the blinded sheet carries no base-case id, so items are resampled)."""
    if len(pairs) < 2:
        return None
    rng, vals = random.Random(seed), []
    for _ in range(b):
        s = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        v = stat([x for x, _ in s], [y for _, y in s])
        if v is not None:
            vals.append(v)
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 4), round(vals[int(0.975 * len(vals)) - 1], 4)] if vals else None


def agreement_stats(la: list[dict], lb: list[dict]) -> dict:
    a = [r["support_label"] for r in la]
    b = [r["support_label"] for r in lb]
    ordinal = [(x, y) for x, y in zip(a, b) if x in ORDINAL and y in ORDINAL]
    rel = [(x["evidence_relevance"], y["evidence_relevance"]) for x, y in zip(la, lb)
           if x["evidence_relevance"] in RELEVANCE and y["evidence_relevance"] in RELEVANCE]
    r4 = lambda v: None if v is None else round(v, 4)
    k4 = lambda x, y: cohen_kappa(x, y, SUPPORT)
    kw = lambda x, y: cohen_kappa(x, y, list(ORDINAL), weighted=True)
    ac = lambda x, y: gwet_ac1(x, y, SUPPORT)
    kr = lambda x, y: cohen_kappa(x, y, RELEVANCE)
    pairs = list(zip(a, b))
    return {
        "n_items": len(a),
        "support_label": {
            "percent_agreement": r4(sum(x == y for x, y in pairs) / len(pairs)),
            "cohen_kappa_unweighted_4": r4(k4(a, b)), "cohen_kappa_unweighted_4_ci95": _boot(pairs, k4),
            "gwet_ac1_4": r4(ac(a, b)), "gwet_ac1_4_ci95": _boot(pairs, ac),
            "cohen_kappa_linear_weighted_3": r4(kw([x for x, _ in ordinal], [y for _, y in ordinal])),
            "cohen_kappa_linear_weighted_3_ci95": _boot(ordinal, kw),
            "weighted_n_items": len(ordinal),
            "weighted_note": "order unsupported < partially_supported < supported; items with not_assessable "
                             "from either annotator are excluded from the weighted statistic only",
            "confusion": {x: {y: sum(1 for p, q in pairs if p == x and q == y) for y in SUPPORT} for x in SUPPORT},
            "marginals_first": {c: a.count(c) for c in SUPPORT}, "marginals_second": {c: b.count(c) for c in SUPPORT}},
        "evidence_relevance": {
            "n_items": len(rel), "percent_agreement": r4(sum(x == y for x, y in rel) / len(rel)) if rel else None,
            "cohen_kappa_unweighted": r4(kr([x for x, _ in rel], [y for _, y in rel])),
            "cohen_kappa_unweighted_ci95": _boot(rel, kr)},
        "ci_method": "item-level percentile bootstrap, 2000 resamples, seed 7"}


ADJ_FIELDS = ["audit_id", "differs", "support_label_A", "support_label_B", "partial_reason_A", "partial_reason_B",
              "evidence_relevance_A", "evidence_relevance_B", "notes_A", "notes_B", "adjudicated_support_label",
              "adjudicated_partial_reason", "adjudicated_evidence_relevance", "resolution", "adjudication_notes"]


def _differs(x: dict, y: dict) -> list[str]:
    d = [f for f in ("support_label", "evidence_relevance") if x[f] != y[f]]
    if x["support_label"] == y["support_label"] == "partially_supported" and x["partial_reason"] != y["partial_reason"]:
        d.append("partial_reason")
    return d


def agreement(first="A", second="B", intra=False):
    check_package()
    la = read_rows(SUBMISSIONS / f"labels_{first}.csv")
    lb = read_rows(SUBMISSIONS / f"labels_{second}.csv")
    common = [r["audit_id"] for r in lb]
    fa = {r["audit_id"]: r for r in la}
    la = [fa[i] for i in common]
    stats = {"first": first, "second": second, "kind": "intra-rater" if intra else "inter-rater",
             "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "inputs": {f"labels_{first}.csv": sha256(SUBMISSIONS / f"labels_{first}.csv"),
                        f"labels_{second}.csv": sha256(SUBMISSIONS / f"labels_{second}.csv")},
             **agreement_stats(la, lb)}
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"agreement_{first}_vs_{second}.json"
    (OUT / name).write_text(json.dumps(stats, indent=1) + "\n")
    print(json.dumps({k: stats[k] for k in ("n_items", "support_label", "evidence_relevance")}, indent=1)[:1500])
    if intra:
        return
    rows = []
    for x, y in zip(la, lb):
        d = _differs(x, y)
        if not d:
            continue
        agreed = lambda f: x[f] if f not in d else ""
        rows.append({"audit_id": x["audit_id"], "differs": ";".join(d),
                     **{f"{f}_{s}": r[f] for f in LABEL_FIELDS for s, r in (("A", x), ("B", y))},
                     "adjudicated_support_label": agreed("support_label"),
                     "adjudicated_partial_reason": x["partial_reason"] if "support_label" not in d
                     and "partial_reason" not in d else "",
                     "adjudicated_evidence_relevance": agreed("evidence_relevance")})
    sheet = ADJ / "adjudication_sheet.csv"
    if sheet.exists():
        raise AuditError(f"{sheet.relative_to(REPO)} exists; it may already hold adjudication decisions")
    write_rows(sheet, ADJ_FIELDS, rows)
    print(f"wrote {OUT.relative_to(REPO) / name} and {sheet.relative_to(REPO)} ({len(rows)} items to adjudicate)")


# ---------------------------------------------------------------- reference labels
REF_FIELDS = ["audit_id", "support_label", "partial_reason", "evidence_relevance", "source"]


def build_reference(la: list[dict], lb: list[dict], adj: list[dict]) -> list[dict]:
    decided = {r["audit_id"]: r for r in adj}
    out, problems = [], []
    for x, y in zip(la, lb):
        d = _differs(x, y)
        if not d:
            out.append({**{f: x[f] for f in REF_FIELDS[1:4]}, "audit_id": x["audit_id"], "source": "agreed"})
            continue
        r = decided.get(x["audit_id"])
        if r is None:
            problems.append(f"{x['audit_id']}: missing from the adjudication sheet")
            continue
        res = r.get("resolution", "")
        if res not in RESOLUTIONS:
            problems.append(f"{x['audit_id']}: resolution must be one of {', '.join(RESOLUTIONS)}")
            continue
        if res == "unresolved":
            out.append({"audit_id": x["audit_id"], "support_label": "unresolved", "partial_reason": "",
                        "evidence_relevance": "", "source": "unresolved"})
            continue
        ref = {"audit_id": x["audit_id"], "support_label": r["adjudicated_support_label"],
               "partial_reason": r["adjudicated_partial_reason"],
               "evidence_relevance": r["adjudicated_evidence_relevance"], "source": f"adjudicated_{res}"}
        problems += [f"{x['audit_id']}: {p}" for p in label_problems(ref)]
        out.append(ref)
    if problems:
        raise AuditError("adjudication sheet is incomplete:\n  " + "\n  ".join(problems[:60]))
    return out


def finalize(single: str | None = None):
    check_package()
    if single:
        rows = read_rows(SUBMISSIONS / f"labels_{single}.csv")
        ref = [{**{f: r[f] for f in REF_FIELDS[:4]}, "source": f"single_annotator_{single}"} for r in rows]
    else:
        la, lb = read_rows(SUBMISSIONS / "labels_A.csv"), read_rows(SUBMISSIONS / "labels_B.csv")
        adj_path = ADJ / "adjudication_sheet.csv"
        ref = build_reference(la, lb, read_rows(adj_path) if adj_path.exists() else [])
    dest = OUT / "reference_labels.csv"
    if dest.exists():
        raise AuditError(f"{dest.relative_to(REPO)} exists; reference labels are written once")
    write_rows(dest, REF_FIELDS, ref)
    meta = {"written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "single_annotator": single,
            "reference_labels_sha256": sha256(dest),
            "sources": {s: sum(r["source"] == s for r in ref) for s in sorted({r["source"] for r in ref})},
            "inputs": {p.name: sha256(p) for p in sorted(SUBMISSIONS.glob("labels_*.csv"))}
            | ({"adjudication_sheet.csv": sha256(ADJ / "adjudication_sheet.csv")}
               if (ADJ / "adjudication_sheet.csv").exists() else {})}
    (OUT / "reference_manifest.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(f"wrote {dest.relative_to(REPO)}; COMMIT it (and reference_manifest.json) before `unseal`")


# ---------------------------------------------------------------- unsealing and analysis
def _committed_and_clean(path: Path) -> bool:
    rel = str(path.relative_to(REPO))
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=REPO, capture_output=True).returncode == 0
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel], cwd=REPO).returncode == 0
    return tracked and clean


def _rate(k, n):
    return None if n == 0 else round(k / n, 4)


def analyse(ref: list[dict], key_rows: list[dict], fill: dict) -> dict:
    """Verifier vs adjudicated human labels. Pure function: inputs are reference labels, key rows, stratum fill."""
    keyd = {k["audit_id"]: k for k in key_rows}
    pos = {"strict": {"supported"}, "lenient": {"supported", "partially_supported"}}
    out = {"definitions": {
        "positive": "human label supported (strict) or supported/partially_supported (lenient); protocol section 9",
        "excluded": "not_assessable and unresolved items are counted and excluded from rates",
        "verified_claim_precision": "share of verifier-accepted sample items that are positive",
        "verified_claim_error_rate": "share of verifier-accepted sample items labelled unsupported (strict) or "
                                     "unsupported/partially_supported (lenient)",
        "rejection_agreement": "share of verifier-rejected sample items that are negative",
        "recall_population": "verifier recall weighted back to each condition's claim population by stratum "
                             "(accepted/rejected availability in audit_manifest.json); approximate because the "
                             "per-base-case cap makes inclusion probabilities slightly unequal within a stratum",
        "hallucination_rate_population": "unsupported + partially_supported among pre-filter claims, stratum-weighted",
        "ci": "cluster bootstrap over base cases within condition, 10000 resamples, seed 7"},
        "conditions": {}}
    for cond in CONDITIONS:
        items = [(r, keyd[r["audit_id"]]) for r in ref if keyd[r["audit_id"]]["condition"] == cond]
        usable = [(r, k) for r, k in items if r["support_label"] in ORDINAL]
        n_acc, n_rej = fill[cond]["accepted_available"], fill[cond]["rejected_available"]

        def stats(sample, variant):
            acc = [r for r, k in sample if k["verifier_accepted"]]
            rej = [r for r, k in sample if not k["verifier_accepted"]]
            P = pos[variant]
            p_acc = sum(r["support_label"] in P for r in acc) / len(acc) if acc else None
            p_rej = sum(r["support_label"] in P for r in rej) / len(rej) if rej else None
            bad = {"unsupported"} if variant == "strict" else {"unsupported", "partially_supported"}
            halluc = lambda rs: sum(r["support_label"] in {"unsupported", "partially_supported"} for r in rs) / len(rs)
            res = {"verified_claim_precision": p_acc,
                   "verified_claim_error_rate": sum(r["support_label"] in bad for r in acc) / len(acc) if acc else None,
                   "rejection_agreement": None if p_rej is None else 1 - p_rej}
            tp, fn = (n_acc * p_acc if p_acc is not None else None), (n_rej * p_rej if p_rej is not None else None)
            res["recall_population"] = None if tp is None or fn is None or tp + fn == 0 else tp / (tp + fn)
            res["hallucination_rate_population"] = (None if not acc or not rej else
                                                     (n_acc * halluc(acc) + n_rej * halluc(rej)) / (n_acc + n_rej))
            yv = [1 if k["verifier_accepted"] else 0 for _, k in sample]
            yh = [1 if r["support_label"] in P else 0 for r, _ in sample]
            res["kappa_verifier_vs_human"] = cohen_kappa(yv, yh, (0, 1))
            return res

        by_case: dict[str, list] = {}
        for r, k in usable:
            by_case.setdefault(k["scenario"], []).append((r, k))
        cases = sorted(by_case)
        cond_out = {"n_items": len(items), "n_usable": len(usable),
                    "n_not_assessable": sum(r["support_label"] == "not_assessable" for r, _ in items),
                    "n_unresolved": sum(r["support_label"] == "unresolved" for r, _ in items),
                    "n_base_cases": len(cases),
                    "stratum_population": {"accepted": n_acc, "rejected": n_rej},
                    "label_counts": {v: {s: sum(r["support_label"] == s for r, k in items
                                                if k["verifier_accepted"] == (v == "accepted"))
                                         for s in (*SUPPORT, "unresolved")} for v in ("accepted", "rejected")}}
        for variant in ("strict", "lenient"):
            point = stats(usable, variant)
            rng, boots = random.Random(BOOT_SEED), {m: [] for m in point}
            for _ in range(BOOT_B if len(cases) > 1 else 0):
                s = [x for _ in cases for x in by_case[cases[rng.randrange(len(cases))]]]
                for m, v in stats(s, variant).items():
                    if v is not None:
                        boots[m].append(v)
            cond_out[variant] = {m: {"estimate": None if v is None else round(v, 4),
                                     "ci95": None if len(boots[m]) < 100 else
                                     [round(sorted(boots[m])[int(0.025 * len(boots[m]))], 4),
                                      round(sorted(boots[m])[int(0.975 * len(boots[m])) - 1], 4)]}
                                 for m, v in point.items()}
        groups = sorted({k["predicate_group"] for _, k in items})
        cond_out["by_predicate_group"] = {g: {v: {s: sum(r["support_label"] == s for r, k in items
                                                         if k["predicate_group"] == g
                                                         and k["verifier_accepted"] == (v == "accepted"))
                                                  for s in (*SUPPORT, "unresolved")}
                                              for v in ("accepted", "rejected")} for g in groups}
        cond_out["relevance_counts"] = {v: {x: sum(r["evidence_relevance"] == x for r, k in items
                                                   if k["verifier_accepted"] == (v == "accepted"))
                                            for x in RELEVANCE} for v in ("accepted", "rejected")}
        sup = [(r, k) for r, k in usable if r["support_label"] == "supported"]
        cond_out["human_supported_about_true_root"] = {"k": sum(bool(k["subject_is_true_root"]) for _, k in sup),
                                                       "n": len(sup)}
        out["conditions"][cond] = cond_out
    if fill["E0"]["rejected_available"] < 60:
        out["caveat_E0_rejected"] = ("E0 has only 59 rejected claims and 39/40 sampled rejected E0 items are "
                                     "onset_before; E0 rejection statistics describe that predicate, not E0 in general")
    return out


def unseal():
    man = check_package()
    ref_path = OUT / "reference_labels.csv"
    if not ref_path.exists():
        raise AuditError("no reference labels; finish annotation, agreement, adjudication and finalize first")
    if not _committed_and_clean(ref_path):
        raise AuditError("reference_labels.csv must be committed (and unchanged) before the key is unsealed")
    ref = read_rows(ref_path)
    if sorted(r["audit_id"] for r in ref) != sorted(r["audit_id"] for r in blinded_items()):
        raise AuditError("reference labels do not cover exactly the 240 audit items")
    if not KEY.exists():
        raise AuditError("audit_key_sealed.json is withheld from the public repository; restore it to unseal")
    key = json.loads(KEY.read_text())
    result = {"unsealed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "reference_labels_sha256": sha256(ref_path), "key_sha256": sha256(KEY),
              "reference_labels_commit": subprocess.run(["git", "log", "-1", "--format=%H", "--",
                                                         str(ref_path.relative_to(REPO))], cwd=REPO,
                                                        capture_output=True, text=True).stdout.strip(),
              **analyse(ref, key["rows"], man["stratum_fill"])}
    (OUT / "audit_analysis.json").write_text(json.dumps(result, indent=1) + "\n")
    print(f"wrote {(OUT / 'audit_analysis.json').relative_to(REPO)}")


# ---------------------------------------------------------------- cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-package")
    w = sub.add_parser("worked-examples")
    w.add_argument("--force", action="store_true")
    p = sub.add_parser("packets")
    p.add_argument("--annotators", nargs="+", default=["A", "B"])
    p.add_argument("--relabel", help="single-annotator fallback: build the seeded 20 %% re-label packet for this id")
    v = sub.add_parser("validate")
    v.add_argument("file", type=Path)
    v.add_argument("--annotator", required=True)
    v.add_argument("--register", action="store_true")
    a = sub.add_parser("agreement")
    a.add_argument("--first", default="A")
    a.add_argument("--second", default="B")
    a.add_argument("--intra", action="store_true")
    f = sub.add_parser("finalize")
    f.add_argument("--single", help="single-annotator fallback: use this annotator's labels as the reference")
    sub.add_parser("unseal")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check-package":
            man = check_package()
            print(f"package OK: {man['n_items']} items, csv {man['csv_sha256'][:12]}, key {man['key_sha256'][:12]} "
                  f"(sealed); worked examples: {'complete' if not worked_examples_complete() else 'NOT complete'}")
        elif args.cmd == "worked-examples":
            worked_examples(args.force)
        elif args.cmd == "packets":
            make_packets(tuple(args.annotators), args.relabel)
        elif args.cmd == "validate":
            if args.register:
                register(args.file, args.annotator)
            else:
                exp = None
                if args.annotator.endswith("-relabel"):
                    exp = [r["audit_id"] for r in read_rows(PACKETS / f"annotator_{args.annotator}"
                                                            / f"labels_{args.annotator}.csv")]
                rows = validate(args.file, exp)
                print(f"{args.file}: valid ({len(rows)} items)")
        elif args.cmd == "agreement":
            agreement(args.first, args.second, args.intra)
        elif args.cmd == "finalize":
            finalize(args.single)
        elif args.cmd == "unseal":
            unseal()
    except AuditError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
