"""Statistics and calibration rules of the frozen final evaluation."""

import math

from evaluation.run_final import choose_tau, cluster_boot, holm, mcnemar_exact, rc, wilson


def test_wilson_known_values():
    lo, hi = wilson(24, 48)
    assert math.isclose(lo, 0.3639, abs_tol=1e-3) and math.isclose(hi, 0.6361, abs_tol=1e-3)
    assert wilson(0, 0) is None and wilson(0, 10)[0] == 0.0


def test_mcnemar_exact():
    assert mcnemar_exact(0, 0) == 1.0
    assert math.isclose(mcnemar_exact(0, 6), 2 * 0.5 ** 6)
    assert mcnemar_exact(3, 3) == 1.0


def test_holm_is_monotone_and_bounded():
    adj = holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj["a"] == 0.03 and adj["c"] == 0.06 and adj["b"] == 0.06


def test_aurc_perfect_and_worst():
    assert rc([(0.9, 0, "a"), (0.1, 1, "b")])[0] == 0.25
    assert rc([(0.9, 1, "a"), (0.1, 0, "b")])[0] == 0.75


def test_choose_tau_rules():
    items = [(s / 10, 1 if s < 3 else 0) for s in range(10)]   # low scores are the errors
    t = choose_tau(items)
    assert t["op1"]["coverage"] >= 0.7 and t["op1"]["selective_risk"] == 0.0
    assert t["op2"]["selective_risk"] <= 0.1
    assert choose_tau([]) == {"op1": None, "op2": None}


def test_cluster_bootstrap_is_deterministic():
    pc = {f"c{i}": float(i % 2) for i in range(20)}
    assert cluster_boot(pc) == cluster_boot(pc)
    lo, hi = cluster_boot(pc)
    assert lo <= 0.5 <= hi
