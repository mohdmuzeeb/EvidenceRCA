"""Abstention signals and gate (protocol §7).

Four transparent signals, each in [0, 1]:
  (i)   evidence sufficiency  - share of the expected evidence streams of the top candidate whose status
                                is determinable in this view (observed, or missing against an established
                                expected-stream baseline); `unavailable` / `no_baseline` streams count against it
  (ii)  modality agreement    - available modalities whose own ranking puts the same top candidate first,
                                divided by the number of available modalities
  (iii) ranking margin        - (s1 - s2) / s1 of the method's own scores
  (iv)  verified-claim coverage - verified observed claims / observed claims (0 if there are none)

S = weighted mean of the four (equal weights by default). S is a heuristic ordering score, NOT a
probability and not calibrated. The gate DIAGNOSEs when S >= tau. tau is configurable; per protocol it
is chosen on the calibration seeds only (not in Phase 3). A-margin uses the margin alone.
"""

from dataclasses import dataclass, field

from evidencerca.signals import SPECS

DEFAULT_WEIGHTS = {"sufficiency": 0.25, "agreement": 0.25, "margin": 0.25, "coverage": 0.25}
# expected evidence streams per service (signal names from evidencerca.signals)
EXPECTED = {
    "app": ("server_latency", "callee_latency", "error_logs", "container_cpu", "latency_mean", "silence"),
    "postgres": ("callee_latency", "slow_statements", "container_cpu", "silence"),
}
DETERMINABLE = ("observed_normal", "observed_anomalous", "missing")
MODALITY_SIGNALS = {
    "trace": ("server_latency", "server_errors", "callee_latency", "callee_failures", "callee_timeouts", "pool_wait"),
    "log": ("error_logs", "slow_statements"),
    "metric": ("container_cpu", "pool_checked_out", "latency_mean", "error_ratio", "silence"),
}


@dataclass
class AbstentionConfig:
    tau: float = 0.5
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


def margin(ranked: list[dict]) -> float:
    if not ranked or ranked[0]["score"] <= 0:
        return 0.0
    s1 = ranked[0]["score"]
    s2 = ranked[1]["score"] if len(ranked) > 1 else 0.0
    return max(0.0, (s1 - s2) / s1)


def sufficiency(signals: dict, service: str) -> float:
    exp = EXPECTED["postgres" if service == "postgres" else "app"]
    st = [signals[service][n].status for n in exp]
    return sum(s in DETERMINABLE for s in st) / len(exp)


def modality_rankings(signals: dict, unavailable) -> dict:
    """Top service per available modality, using only that modality's anomalous signals (max strength)."""
    out = {}
    for mod, names in MODALITY_SIGNALS.items():
        if mod in unavailable:
            continue
        scored = sorted(((-max((sig[n].strength for n in names if n in sig and sig[n].anomalous), default=0.0),
                          svc) for svc, sig in signals.items()))
        out[mod] = scored[0][1] if scored and scored[0][0] < 0 else None
    return out


def agreement(top: str, per_modality: dict) -> float:
    if not per_modality:
        return 0.0
    return sum(v == top for v in per_modality.values()) / len(per_modality)


def score(diag: dict, signals: dict | None, verification: dict | None, unavailable, cfg: AbstentionConfig) -> dict:
    top = diag.get("suspected_service")
    if top is None:
        return {"S": 0.0, "S_margin": 0.0, "components": None, "decision": "ABSTAIN",
                "decision_margin": "ABSTAIN", "reason": "no candidate"}
    comp = {"margin": margin(diag["ranked_candidates"])}
    if signals is not None:
        comp["sufficiency"] = sufficiency(signals, top)
        per_mod = modality_rankings(signals, unavailable)
        comp["agreement"] = agreement(top, per_mod)
        comp["per_modality_top"] = per_mod
    else:
        comp["sufficiency"] = comp["agreement"] = None
    cov = (verification or {}).get("summary", {}).get("verified_claim_coverage")
    comp["coverage"] = cov if cov is not None else (0.0 if verification is not None else None)
    keys = [k for k in cfg.weights if comp.get(k) is not None]
    w = sum(cfg.weights[k] for k in keys)
    s = sum(cfg.weights[k] * comp[k] for k in keys) / w if w else 0.0
    return {"S": round(s, 6), "S_margin": round(comp["margin"], 6), "components": comp, "signals_used": keys,
            "decision": "DIAGNOSE" if s >= cfg.tau else "ABSTAIN",
            "decision_margin": "DIAGNOSE" if comp["margin"] >= cfg.tau else "ABSTAIN",
            "tau": cfg.tau, "note": "S is a heuristic ordering score, not a probability"}


__all__ = ["AbstentionConfig", "score", "margin", "sufficiency", "agreement", "modality_rankings", "SPECS"]
