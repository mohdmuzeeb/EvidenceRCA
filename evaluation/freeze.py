"""Protocol freeze: hash every file that determines capture or evaluation, and refuse to run calibration/test
capture or the final evaluation if any of them changed.

  uv run python -m evaluation.freeze write    # create docs/frozen/freeze_manifest.json (done once, then committed)
  uv run python -m evaluation.freeze verify   # check the working tree against the committed manifest
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "frozen" / "freeze_manifest.json"
FROZEN_GLOBS = [
    "compose.yaml", "uv.lock", "pyproject.toml", "testbed/Dockerfile", "testbed/requirements.lock",
    "testbed/otel-collector.yaml", "testbed/postgres/init.sql", "testbed/app/*.py",
    "harness/*.py", "harness/vm/*.py", "scripts/run_phase2.py",
    "evidencerca/*.py", "evidencerca/methods/*.py",
    "evaluation/views.py", "evaluation/run_dev.py", "evaluation/run_final.py", "evaluation/freeze.py",
    "docs/experiment_protocol.md", "docs/frozen/frozen_protocol.md",
]
SPLITS = {"dev": [11], "calibration": [23, 37], "test": [41, 53, 67]}


class FreezeViolation(RuntimeError):
    pass


def current_hashes() -> dict:
    out = {}
    for pattern in FROZEN_GLOBS:
        for p in sorted(ROOT.glob(pattern)):
            if p.is_file() and "__pycache__" not in p.parts:
                out[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def verify_frozen() -> dict:
    if not MANIFEST.exists():
        raise FreezeViolation("no freeze manifest: the protocol is not frozen")
    man = json.loads(MANIFEST.read_text())
    cur = current_hashes()
    changed = sorted(k for k in man["files"] if cur.get(k) != man["files"][k])
    added = sorted(set(cur) - set(man["files"]))
    if changed or added:
        raise FreezeViolation(f"frozen files changed: {changed}; unfrozen additions: {added}")
    return man


def write() -> None:
    from harness.schedule import single_fault_matrix, PROTOCOL_TIMELINE
    from harness.faults import fault_for, NETEM_DELAY_MS, PROCESSING_DELAY_MS, POOL_SIZE, CPU_BURNERS, STRONG_CPU_CAP
    from evaluation.views import SPECS
    scenarios = {}
    for split, seeds in SPLITS.items():
        for seed in seeds:
            variant = "standard" if split == "dev" else "strong"
            scenarios[str(seed)] = {"split": split, "cpu_variant": variant,
                                    "scenarios_in_execution_order": [s.key for s in single_fault_matrix(seed, True, variant)]}
    man = {
        "schema": "evidencerca.freeze_manifest/v1",
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "splits": SPLITS,
        "use": {"dev": "development only (done; Phases 2-3)",
                "calibration": "abstention threshold tau only (protocol §7); no other tuning",
                "test": "held-out evaluation; labels read only by evaluation/run_final.py evaluate, after calibration"},
        "capture": {"timeline_s": vars(PROTOCOL_TIMELINE), "workload": {"rate_rps": 10.0, "concurrency": 32},
                    "fault_parameters": {"processing_delay_ms": PROCESSING_DELAY_MS, "netem_delay_ms": NETEM_DELAY_MS,
                                         "pool_held": POOL_SIZE, "cpu_burners": CPU_BURNERS,
                                         "strong_cpu_cap": STRONG_CPU_CAP,
                                         "cpu_variant_for_calibration_and_test": "strong"},
                    "dual_fault_scenarios": "not captured (optional in protocol §3; not implemented)"},
        "scenarios": scenarios,
        "views_per_case": [f"{s}:{k}" for s, k in SPECS],
        "methods": ["B1", "B2", "B3", "E0", "E0-A-graph"],
        "abstention": {"score": "equal-weight mean of sufficiency, agreement, margin, verified-claim coverage",
                       "ablations": ["A-margin (margin only)", "A-ver (S without verified-claim coverage)",
                                     "A-abst (never abstain)", "A-graph (E0 without graph constraints)"],
                       "tau_rule": "on calibration seeds only: op1 = min selective risk s.t. coverage >= 0.7; "
                                   "op2 = max coverage s.t. selective risk <= 0.1 (if achievable)"},
        "files": current_hashes(),
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(man, indent=1) + "\n")
    print(f"manifest with {len(man['files'])} frozen files -> {MANIFEST}")


if __name__ == "__main__":
    if sys.argv[1:] == ["write"]:
        write()
    elif sys.argv[1:] == ["verify"]:
        m = verify_frozen()
        print(f"frozen tree OK ({len(m['files'])} files, frozen at {m['frozen_at']})")
    else:
        raise SystemExit(__doc__)
