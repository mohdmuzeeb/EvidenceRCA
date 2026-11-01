"""Manuscript preparation: everything recorded before it stays byte-identical, and the paper artifacts are
reproducible from the recorded results (no experiment, no model call)."""
import hashlib
import json
from pathlib import Path

from evaluation.freeze import verify_frozen
from tests.withheld import present

REPO = Path(__file__).resolve().parents[1]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_frozen_tree_unchanged():
    verify_frozen()


def test_everything_tracked_before_manuscript_prep_is_unchanged():
    lock = json.loads((REPO / "results/manuscript_prep/preserved_before_manuscript_prep.json").read_text())
    assert lock["source_commit"] == "12af836" and len(lock["files"]) == 320
    changed = [f for f, h in lock["files"].items() if present(f) and _sha(REPO / f) != h]
    assert not changed, changed


def test_audit_package_still_sealed_and_unlabelled():
    man = json.loads((REPO / "results/phase4b/audit_manifest.json").read_text())
    assert _sha(REPO / "results/phase4b/audit_blinded.csv") == man["csv_sha256"]
    if present("results/phase4b/audit_key_sealed.json"):
        assert _sha(REPO / "results/phase4b/audit_key_sealed.json") == man["key_sha256"]
    assert man["status"].startswith("PENDING")
    # no human-label outputs exist yet: no registered submissions, no reference labels, no unsealed analysis
    for p in ("audit/submissions", "audit/results/reference_labels.csv", "audit/results/audit_analysis.json"):
        assert not (REPO / p).exists(), p


def test_paper_artifacts_rebuild_identically(tmp_path, monkeypatch):
    from paper import build_artifacts as ba
    committed = json.loads((REPO / "paper/artifact_manifest.json").read_text())
    for rel, h in committed["inputs"].items():   # built from the recorded results as they are now
        assert _sha(REPO / rel) == h, rel
    paper = tmp_path / "paper"
    monkeypatch.setattr(ba, "REPO", tmp_path)          # outputs are keyed relative to REPO
    monkeypatch.setattr(ba, "PAPER", paper)
    monkeypatch.setattr(ba, "TABLES", paper / "tables")
    monkeypatch.setattr(ba, "FIGURES", paper / "figures")
    paper.mkdir()
    for f in ("build_artifacts.py", "svgfig.py"):
        (paper / f).write_bytes((REPO / "paper" / f).read_bytes())
    for k, rel in ba.INPUTS.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).symlink_to(REPO / rel)
    man = ba.build(convert=False)
    assert man["outputs"] == committed["outputs"]
    assert man["generator_sha256"] == committed["generator_sha256"]


def test_tables_carry_their_evidence_tier():
    for p in sorted((REPO / "paper/tables").glob("*.tex")):
        text = p.read_text()
        tier = "Confirmatory" if p.name.startswith("conf_") else "Exploratory"
        assert f"\\caption{{{tier} (" in text, p.name
    numbers = (REPO / "paper/numbers.tex").read_text()
    assert "\\newcommand{\\numEzeroCzeroTopOne}{0.81}" in numbers
    assert "\\newcommand{\\numAURCS}{0.214}" in numbers          # half-up, as in docs/final_results.md
    assert "\\newcommand{\\numClaimsPairedCI}{[$-$0.094, 0.018]}" in numbers


def test_key_numbers_match_recorded_results():
    md = (REPO / "paper/tables/README.md").read_text()
    for frag in ("| C0 | 47 | .26 [.15,.40] | .53 [.39,.67] |", "38/47", "74 (0.31)", "148 (0.63)",
                 "30 [0.89, 1.00]", "454/1564", "369/1452", "1023/1082", "−0.06 [−0.21, 0.08]",
                 "| Cascading failure | 3 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 |", "0.214 vs 0.160"):
        assert frag in md, frag
