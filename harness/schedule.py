"""Pre-registered scenario matrix and seed splits (docs/experiment_protocol.md §3), and the Phase 2 pilot.

Split definitions are fixed here and must not change: seed 11 = dev, 23/37 = calibration, 41/53/67 = test.
Running a non-dev seed requires an explicit flag, so test cases cannot be captured or looked at by accident.
"""

import hashlib
import random
from dataclasses import asdict, dataclass

SPLITS = {11: "dev", 23: "calibration", 37: "calibration", 41: "test", 53: "test", 67: "test"}
ELIGIBLE = {
    "service_crash": ["order", "payment", "inventory"],
    "processing_latency": ["gateway", "order", "payment", "inventory"],
    "network_timeout": ["order", "payment", "inventory"],
    "db_pool_exhaustion": ["order", "inventory"],
    "cpu_pressure": ["order", "payment", "inventory"],
    "cascading_failure": ["postgres"],
}
CONTROLS_PER_SEED = 2
PILOT_FAULTS = ["none", "processing_latency", "service_crash", "db_pool_exhaustion"]


@dataclass(frozen=True)
class Timeline:
    warmup_s: float = 30.0
    baseline_s: float = 90.0
    injection_s: float = 120.0
    recovery_s: float = 60.0
    max_onset_jitter_s: float = 10.0


PROTOCOL_TIMELINE = Timeline()
MECHANISM_CHECK_TIMELINE = Timeline(warmup_s=15, baseline_s=40, injection_s=45, recovery_s=40, max_onset_jitter_s=0)


@dataclass(frozen=True)
class Scenario:
    master_seed: int
    split: str
    fault_type: str
    target: str
    replicate: int = 0

    @property
    def key(self) -> str:
        return f"{self.master_seed}:{self.fault_type}:{self.target}:{self.replicate}"

    def derived_int(self, purpose: str) -> int:
        return int(hashlib.sha256(f"{self.key}:{purpose}".encode()).hexdigest()[:8], 16)

    @property
    def workload_seed(self) -> int:
        return self.derived_int("workload")

    def onset_jitter_s(self, timeline: Timeline) -> float:
        return round(random.Random(self.derived_int("jitter")).uniform(0, timeline.max_onset_jitter_s), 3)

    def as_dict(self) -> dict:
        return {**asdict(self), "key": self.key, "workload_seed": self.workload_seed}


def split_of(seed: int, allow_non_dev: bool = False) -> str:
    if seed not in SPLITS:
        raise ValueError(f"seed {seed} is not one of the pre-registered seeds {sorted(SPLITS)}")
    if SPLITS[seed] != "dev" and not allow_non_dev:
        raise PermissionError(f"seed {seed} belongs to the {SPLITS[seed]} split; capture needs explicit approval "
                              "(--allow-non-dev-seed)")
    return SPLITS[seed]


def single_fault_matrix(seed: int, allow_non_dev: bool = False) -> list[Scenario]:
    """The 16 single-fault + 2 control scenarios of one seed, in seeded execution order."""
    split = split_of(seed, allow_non_dev)
    scenarios = [Scenario(seed, split, f, t) for f, targets in ELIGIBLE.items() for t in targets]
    scenarios += [Scenario(seed, split, "none", "-", replicate=r) for r in range(CONTROLS_PER_SEED)]
    random.Random(f"order:{seed}").shuffle(scenarios)
    return scenarios


def pilot(seed: int = 11) -> list[Scenario]:
    """Phase 2 pilot: control + latency + crash + pool exhaustion, targets drawn by seed from the matrix."""
    matrix = single_fault_matrix(seed)
    rng = random.Random(f"pilot:{seed}")
    chosen = []
    for fault in PILOT_FAULTS:
        candidates = sorted((s for s in matrix if s.fault_type == fault), key=lambda s: s.key)
        chosen.append(rng.choice(candidates))
    return chosen
