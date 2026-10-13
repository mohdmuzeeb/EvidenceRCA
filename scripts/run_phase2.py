"""Phase 2 driver: pilot cases, per-fault mechanism checks and a sustained-load measurement.

  uv run python scripts/run_phase2.py pilot --label A        # 4 pilot cases, protocol timeline, seed 11 (dev)
  uv run python scripts/run_phase2.py mechanism              # all six faults, short timeline (not experiment data)
  uv run python scripts/run_phase2.py load --rate 20         # no-fault sustained load, collector headroom
  uv run python scripts/run_phase2.py summarize              # rebuild results/phase2/*.json from ground truth

Captures run Docker only; never start Ollama while this runs (8 GB host).
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from evidencerca.paths import CASES_ROOT, DATA_ROOT, GROUND_TRUTH_ROOT
from harness.case import run_case
from harness.schedule import (MECHANISM_CHECK_TIMELINE, PROTOCOL_TIMELINE, Scenario, Timeline, pilot,
                              single_fault_matrix, split_of)

RESULTS = Path("results/phase2")
INDEX = DATA_ROOT / "harness_index.jsonl"   # case_id -> run group/label; harness side only
# Mechanism checks use fixed targets, chosen to differ from the seeded pilot draw where possible.
MECHANISM_TARGETS = {"processing_latency": "payment", "service_crash": "order", "db_pool_exhaustion": "order",
                     "network_timeout": "payment", "cpu_pressure": "order", "cascading_failure": "postgres"}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def keep_awake():
    # macOS idle sleep pauses the VM (Phase 1 finding); hold an assertion for this process's lifetime.
    return subprocess.Popen(["caffeinate", "-is", "-w", str(__import__("os").getpid())])


def record(group: str, label: str, gt: dict) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX, "a") as f:
        f.write(json.dumps({"case_id": gt["case_id"], "group": group, "label": label,
                            "scenario": gt["scenario"]["key"], "case_valid": gt["case_valid"]}) + "\n")


def cmd_pilot(args) -> int:
    split = split_of(args.seed)
    log(f"pilot: seed {args.seed} ({split}), label {args.label}")
    rc = 0
    for sc in pilot(args.seed):
        if args.only and sc.fault_type not in args.only:
            continue
        gt = run_case(sc, PROTOCOL_TIMELINE, CASES_ROOT, GROUND_TRUTH_ROOT, rate=args.rate, log=log)
        record("pilot", args.label, gt)
        rc |= not gt["case_valid"]
    return rc


def cmd_matrix(args) -> int:
    """Capture the single-fault + control matrix of one seed (dev only unless explicitly allowed), skipping
    scenarios that already have a valid protocol-timeline capture in the experiment index."""
    split = split_of(args.seed, allow_non_dev=args.allow_non_dev_seed)
    if split != "dev":
        from evaluation.freeze import verify_frozen   # capture of calibration/test data requires the frozen code
        verify_frozen()
    done = set()
    if INDEX.exists():
        for line in INDEX.read_text().splitlines():
            e = json.loads(line)
            if e["group"] in ("pilot", "matrix", "calibration", "test") and e["case_valid"]:
                done.add(e["scenario"])
    todo = [s for s in single_fault_matrix(args.seed, args.allow_non_dev_seed, args.cpu_variant) if s.key not in done]
    log(f"matrix: seed {args.seed} ({split}): {len(todo)} scenarios to capture, {len(done)} already captured")
    rc = 0
    for sc in todo:
        gt = run_case(sc, PROTOCOL_TIMELINE, CASES_ROOT, GROUND_TRUTH_ROOT, rate=args.rate, log=log)
        record("matrix" if split == "dev" else split, args.label, gt)
        rc |= not gt["case_valid"]
    return rc


def cmd_cpu_variant(args) -> int:
    """Phase 3: strong cpu_pressure variant on the dev seed, protocol timeline, every eligible target."""
    split = split_of(args.seed)
    rc = 0
    for target in ("order", "payment", "inventory"):
        sc = Scenario(args.seed, split, "cpu_pressure", target, replicate=0, variant="strong")
        if args.only and target not in args.only:
            continue
        gt = run_case(sc, PROTOCOL_TIMELINE, CASES_ROOT, GROUND_TRUTH_ROOT, rate=args.rate, log=log)
        record("cpu_variant", args.label, gt)
        rc |= not gt["case_valid"]
    return rc


def cmd_mechanism(args) -> int:
    rc = 0
    for fault, target in MECHANISM_TARGETS.items():
        if args.only and fault not in args.only:
            continue
        sc = Scenario(11, split_of(11), fault, target, replicate=900)  # replicate 900+: mechanism checks
        gt = run_case(sc, MECHANISM_CHECK_TIMELINE, DATA_ROOT / "mechanism" / "cases",
                      DATA_ROOT / "mechanism" / "ground_truth", rate=args.rate, log=log)
        record("mechanism", args.label, gt)
        rc |= not gt["case_valid"]
    return rc


def cmd_load(args) -> int:
    tl = Timeline(warmup_s=20, baseline_s=args.duration / 2, injection_s=args.duration / 2, recovery_s=20,
                  max_onset_jitter_s=0)
    sc = Scenario(11, split_of(11), "none", "-", replicate=950 + int(args.rate))
    gt = run_case(sc, tl, DATA_ROOT / "load" / "cases", DATA_ROOT / "load" / "ground_truth", rate=args.rate,
                  concurrency=args.concurrency, log=log)
    record("load", args.label or f"rate-{args.rate:g}", gt)
    return 0 if gt["case_valid"] else 1


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pilot")
    a.add_argument("--seed", type=int, default=11)
    a.add_argument("--label", required=True)
    a.add_argument("--rate", type=float, default=10.0)
    a.add_argument("--only", nargs="*")
    m = sub.add_parser("mechanism")
    m.add_argument("--label", default="mechanism")
    m.add_argument("--rate", type=float, default=10.0)
    m.add_argument("--only", nargs="*")
    lo = sub.add_parser("load")
    lo.add_argument("--rate", type=float, default=20.0)
    lo.add_argument("--duration", type=float, default=120.0)
    lo.add_argument("--concurrency", type=int, default=64)
    lo.add_argument("--label")
    mx = sub.add_parser("matrix")
    mx.add_argument("--seed", type=int, default=11)
    mx.add_argument("--label", default="dev-matrix")
    mx.add_argument("--rate", type=float, default=10.0)
    mx.add_argument("--allow-non-dev-seed", action="store_true",
                    help="required for calibration/test seeds (approved 2026-10-13; frozen code is verified first)")
    mx.add_argument("--cpu-variant", default="standard", choices=["standard", "strong"])
    cv = sub.add_parser("cpu-variant")
    cv.add_argument("--seed", type=int, default=11)
    cv.add_argument("--label", default="cpu-strong")
    cv.add_argument("--rate", type=float, default=10.0)
    cv.add_argument("--only", nargs="*")
    sub.add_parser("summarize")
    args = p.parse_args()
    if args.cmd == "summarize":
        from scripts.summarize_phase2 import main as summarize
        return summarize()
    awake = keep_awake()
    try:
        return {"pilot": cmd_pilot, "mechanism": cmd_mechanism, "load": cmd_load, "matrix": cmd_matrix,
                "cpu-variant": cmd_cpu_variant}[args.cmd](args)
    finally:
        awake.terminate()


if __name__ == "__main__":
    sys.exit(main())
