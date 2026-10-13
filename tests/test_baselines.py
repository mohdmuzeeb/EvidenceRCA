"""Baselines and pipeline on synthetic evidence: cyclic / disconnected graphs, missing modalities, empty evidence."""

import math

import pytest

from evidencerca.core import detect_alert, make_windows, observed_graph
from evidencerca.evidence import View, assign_ids
from evidencerca.methods.baselines import b1, b2, b3, microrca_ppv
from evidencerca.methods.evidencerca_e0 import diagnose
from tests.synth import T0, SEC, make_records, make_view

METHODS = (b1, b2, b3, diagnose)


def windows_for(view):
    w = detect_alert(view)
    return w if w.triggered else make_windows(view, T0 + 150 * SEC, "oracle")


# ---------------------------------------------------------------- MicroRCA Eq. 4 PPV
def test_ppv_cyclic_graph_converges():
    nodes = ["a", "b", "c"]
    edges = [("a", "b"), ("b", "c"), ("c", "a")]
    v = microrca_ppv(nodes, edges, {e: 1.0 for e in edges}, {"b": 1.0})
    assert all(math.isfinite(x) and x >= 0 for x in v.values()) and v["b"] == max(v.values())


def test_ppv_disconnected_graph_isolated_component_gets_nothing():
    nodes = ["a", "b", "x", "y"]
    edges = [("a", "b"), ("x", "y")]
    v = microrca_ppv(nodes, edges, {e: 1.0 for e in edges}, {"b": 1.0})
    assert v["x"] == 0 and v["y"] == 0 and v["b"] > 0


def test_ppv_scores_flow_from_callers_to_callee():
    # reversed edges: callee b links to callers a1, a2; b's score averages its callers' scores
    nodes = ["a1", "a2", "b"]
    edges = [("a1", "b"), ("a2", "b")]
    v = microrca_ppv(nodes, edges, {e: 1.0 for e in edges}, {"a1": 1.0, "a2": 1.0})
    assert v["b"] > 0


def test_ppv_empty_and_zero_preference():
    assert microrca_ppv([], [], {}, {}) == {}
    v = microrca_ppv(["a", "b"], [("a", "b")], {("a", "b"): 1.0}, {})
    assert all(x > 0 for x in v.values())


# ---------------------------------------------------------------- empty evidence
def test_empty_view_no_alert_and_every_method_abstains():
    v = View("vw-empty", "00000000-0000-4000-8000-000000000000", [])
    w = detect_alert(v)
    assert not w.triggered
    for m in METHODS:
        d = m(v, w)
        assert d["decision"] == "ABSTAIN" and d["suspected_service"] is None


def test_empty_view_with_forced_window_does_not_crash():
    v = View("vw-empty", "00000000-0000-4000-8000-000000000000", [])
    w = make_windows(v, 120 * SEC, "oracle")
    for m in METHODS:
        d = m(v, w)
        assert d["decision"] == "ABSTAIN"


# ---------------------------------------------------------------- missing modalities
@pytest.mark.parametrize("drop", [("trace",), ("log",), ("metric",), ("trace", "log")])
def test_missing_modalities_handled(drop):
    v = make_view("latency", "payment", drop_modalities=drop)
    w = windows_for(v)
    for m in METHODS:
        d = m(v, w)
        assert d["missing_modalities"] == sorted(drop)
        if "trace" in drop and m in (b2, b3):
            assert d["decision"] == "ABSTAIN" and d["uncertainty_reasons"]


# ---------------------------------------------------------------- graphs
def test_cyclic_service_graph_terminates():
    recs = make_records("latency", "payment")
    # add a reverse call payment -> order so the observed graph has a cycle order <-> payment
    extra = [dict(r, service="payment", peer_service="order", span_id=f"f{i:015x}", evidence_id=None)
             for i, r in enumerate(x for x in recs if x["kind"] == "CLIENT" and x["peer_service"] == "payment")]
    v = View("vw-cyc", recs[0]["case_id"], assign_ids(recs + extra, recs[0]["case_id"]))
    g = observed_graph(v)
    assert ("order", "payment") in g and ("payment", "order") in g
    w = windows_for(v)
    for m in METHODS:
        m(v, w)   # must terminate without error


def test_disconnected_graph_without_client_spans():
    recs = [r for r in make_records("latency", "payment") if r["kind"] != "CLIENT"]
    v = View("vw-disc", recs[0]["case_id"], recs)
    assert observed_graph(v) == {}
    w = windows_for(v)
    for m in METHODS:
        d = m(v, w)
        assert d["method"] in ("B1", "B2", "B3", "E0")


# ---------------------------------------------------------------- behaviour on clear faults
def test_e0_localizes_synthetic_faults():
    for fault, target, ftype in [("latency", "payment", "processing_latency"), ("crash", "inventory", "service_crash"),
                                 ("pool", "order", "db_pool_exhaustion")]:
        v = make_view(fault, target)
        d = diagnose(v, windows_for(v))
        assert d["suspected_service"] == target, (fault, d["ranked_candidates"][:3])
        assert d["suspected_fault_type"] == ftype


def test_methods_are_deterministic():
    v = make_view("latency", "payment")
    w = windows_for(v)
    for m in METHODS:
        a, b = m(v, w), m(v, w)
        a.pop("_internal", None), b.pop("_internal", None)
        assert a == b


def test_healthy_synthetic_case_raises_no_alert():
    assert not detect_alert(make_view()).triggered
