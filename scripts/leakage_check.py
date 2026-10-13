"""Run every label-leakage check over the captured cases and write results/phase2/leakage_test_results.json.

  uv run python -m scripts.leakage_check            # experiment cases (data/cases) + mechanism checks
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from evidencerca import paths
from harness import leakage

OUT = Path("results/phase2/leakage_test_results.json")


def case_sets() -> list[tuple[str, Path, Path]]:
    return [("experiment", paths.CASES_ROOT, paths.GROUND_TRUTH_ROOT),
            ("mechanism", paths.DATA_ROOT / "mechanism" / "cases", paths.DATA_ROOT / "mechanism" / "ground_truth"),
            ("load", paths.DATA_ROOT / "load" / "cases", paths.DATA_ROOT / "load" / "ground_truth")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    out_path = Path(ap.parse_args().out)
    report = {"schema": "evidencerca.phase2.leakage/v1",
              "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "forbidden_tokens": leakage.FORBIDDEN, "case_sets": {}}
    all_ok = True
    first_experiment = None
    bundles_all = []
    for name, cases_root, gt_root in case_sets():
        if not cases_root.exists():
            continue
        entries = []
        for case_dir in sorted(p for p in cases_root.iterdir() if p.is_dir()):
            gt_file = gt_root / case_dir.name / "ground_truth.json"
            if not gt_file.exists():
                entries.append({"case_id": case_dir.name, "passed": False, "issue": "no ground truth"})
                continue
            gt = json.loads(gt_file.read_text())
            b = leakage.check_bundle(case_dir, gt)
            b["builder"] = leakage.check_builder_output(case_dir.name, gt, cases_root)
            b["case_id_is_uuid4"] = bool(leakage.rca_input.UUID4.match(case_dir.name))
            b["ground_truth_outside_cases_root"] = not str(gt_file.resolve()).startswith(str(cases_root.resolve()))
            b["passed"] = b["passed"] and b["builder"]["passed"] and b["case_id_is_uuid4"] \
                and b["ground_truth_outside_cases_root"]
            entries.append(b)
            bundles_all.append(b)
            if name == "experiment" and first_experiment is None:
                first_experiment = case_dir.name
        ok = all(e["passed"] for e in entries)
        all_ok &= ok
        report["case_sets"][name] = {"cases": len(entries), "passed": ok, "results": entries}

    if first_experiment:
        guard = leakage.check_path_guard(first_experiment, paths.CASES_ROOT, paths.GROUND_TRUTH_ROOT)
        report["path_guard_attacks"] = {"results": guard, "passed": all(g["blocked"] for g in guard)}
        muts = leakage.check_mutations(first_experiment, paths.CASES_ROOT)
        report["scanner_mutation_tests"] = {"results": muts, "passed": all(m["detected"] for m in muts)}
        all_ok &= report["path_guard_attacks"]["passed"] and report["scanner_mutation_tests"]["passed"]
    report["static_import_check"] = leakage.check_static_imports(Path("."))
    report["cross_case_consistency"] = leakage.cross_case_consistency(bundles_all) if bundles_all else None
    all_ok &= report["static_import_check"]["passed"] and bool(bundles_all) and \
        report["cross_case_consistency"]["passed"]

    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_leakage.py"], capture_output=True,
                         text=True)
    report["pytest_tests_test_leakage"] = {"returncode": res.returncode,
                                           "summary": res.stdout.strip().splitlines()[-1] if res.stdout else ""}
    all_ok &= res.returncode == 0
    report["passed"] = all_ok
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for name, cs in report["case_sets"].items():
        print(f"{name}: {cs['cases']} cases, passed={cs['passed']}")
    for key in ("path_guard_attacks", "scanner_mutation_tests", "static_import_check", "cross_case_consistency"):
        if report.get(key):
            print(f"{key}: passed={report[key]['passed']}")
    print(f"pytest tests/test_leakage.py: {report['pytest_tests_test_leakage']['summary']}")
    print(f"leakage checks {'PASSED' if all_ok else 'FAILED'} -> {out_path}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
