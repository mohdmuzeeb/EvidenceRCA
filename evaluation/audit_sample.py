"""Prepare a blinded human-audit sheet of observed claims (docs/evidence_verifier_spec.md §5.2).

The sheet shows each claim and its cited records with nearby context, but hides the method and the verifier
verdict; those go to a separate key file. Labels must be entered by a person; this tool never produces labels.

  uv run python -m evaluation.audit_sample --trials results/phase3/dev_results.json --n 200 --out runs/audit
  (Phase 3 provides the tool only; the real audit uses test-seed outputs and is not performed here.)
"""

import argparse
import json
import random
from pathlib import Path


def build_sheet(items: list[dict], n: int, seed: int = 20261012) -> tuple[list[dict], list[dict]]:
    """items: dicts with claim, cited_records, context_records, method, verdict, stratum. Returns (sheet, key)."""
    rng = random.Random(seed)
    strata = {}
    for it in items:
        strata.setdefault(it["stratum"], []).append(it)
    picked = []
    per = max(1, n // max(1, len(strata)))
    for k in sorted(strata):
        pool = strata[k][:]
        rng.shuffle(pool)
        picked.extend(pool[:per])
    rng.shuffle(picked)
    sheet, key = [], []
    for i, it in enumerate(picked[:n]):
        aid = f"A{i:04d}"
        c = it["claim"]
        sheet.append({"audit_id": aid, "subject_service": c["subject_service"], "object_service": c.get("object_service"),
                      "predicate": c["predicate"], "value": c["value"], "unit": c["unit"], "window": c["window"],
                      "cited_records": it["cited_records"], "context_records": it["context_records"],
                      "label": None, "annotator_notes": None})
        key.append({"audit_id": aid, "method": it["method"], "verifier_verdict": it["verdict"], "stratum": it["stratum"]})
    return sheet, key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True, help="JSON list of audit items (claim, records, method, verdict, stratum)")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sheet, key = build_sheet(json.loads(Path(a.items).read_text()), a.n)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "audit_sheet.json").write_text(json.dumps(sheet, indent=1))
    (out / "audit_key_DO_NOT_SHOW_ANNOTATOR.json").write_text(json.dumps(key, indent=1))
    print(f"{len(sheet)} claims -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
