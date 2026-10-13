"""Leakage regression tests for evidence views (Phase 3)."""

import dataclasses
import re

import pytest

from evaluation.views import SPECS, build
from evidencerca.evidence import RECORD_KEYS, View
from harness.leakage import check_static_imports, scan_tokens
from tests.synth import T0, SEC, make_records

CASE = "00000000-0000-4000-8000-000000000000"
GT = {"fault_type": "processing_latency", "primary_root_services": ["payment"],
      "timeline_vm_ns": {"injection_start_ns": T0 + 150 * SEC, "planned_injection_start_ns": T0 + 150 * SEC}}


@pytest.fixture(scope="module")
def views():
    clean = make_records("latency", "payment")
    return clean, {(s, k): build(clean, CASE, s, k, GT) for s, k in SPECS}


def test_eighteen_views(views):
    assert len(views[1]) == 18


def test_view_exposes_only_allowlisted_fields(views):
    names = {f.name for f in dataclasses.fields(View)}
    # by_id and _idx are lookup structures derived from `records` only; they add no information
    assert names == {"view_id", "case_id", "records", "unavailable", "time_tolerance_ns", "by_id", "_idx"}


def test_view_ids_are_opaque_and_unique(views):
    ids = [v.view_id for v, _ in views[1].values()]
    assert len(set(ids)) == len(ids)
    for vid in ids:
        assert re.fullmatch(r"vw-[0-9a-f]{16}", vid)
        assert not scan_tokens(vid.encode())


def test_records_keep_the_exact_schema_including_decoys(views):
    for (spec, _), (v, _) in views[1].items():
        for r in v.records:
            assert tuple(r) == RECORD_KEYS or set(r) == set(RECORD_KEYS), spec


def test_decoy_records_are_indistinguishable(views):
    clean, vs = views
    cf, log = vs[("CF", 1)]
    assert log["decoy_service"] not in GT["primary_root_services"] and log["shifted_records"] > 0
    clean_ids = {r["evidence_id"] for r in clean}
    new = [r for r in cf.records if r["evidence_id"] not in clean_ids]
    assert new, "decoy records must exist"
    for r in new:
        assert re.fullmatch(r"ev-[0-9a-f]{20}", r["evidence_id"]) and r["source"] in ("otlp", "docker_stats")
    blob = repr([r for r in cf.records]).encode()
    assert not scan_tokens(blob) and b"decoy" not in blob.lower() and b"synthetic" not in blob.lower()


def test_transform_log_never_reaches_the_view(views):
    for (spec, _), (v, log) in views[1].items():
        blob = repr(dataclasses.asdict(v)).lower()
        for leaked in ("decoy", "roots", "perturbation", spec.lower() + "|"):
            assert leaked not in blob, (spec, leaked)


def test_views_are_deterministic():
    clean = make_records("latency", "payment")
    for spec, seed in SPECS:
        a, _ = build(clean, CASE, spec, seed, GT)
        b, _ = build(clean, CASE, spec, seed, GT)
        assert a.view_id == b.view_id and [r["evidence_id"] for r in a.records] == [r["evidence_id"] for r in b.records]


def test_perturbations_do_what_they_say(views):
    clean, vs = views
    r40, _ = vs[("R40", 1)]
    assert 0.5 < len(r40.records) / len(clean) < 0.7
    mt, _ = vs[("MT", 1)]
    assert "trace" in mt.unavailable and not any(r["modality"] == "trace" for r in mt.records)
    cs, _ = vs[("CS", 1)]
    assert not any(r["modality"] == "trace" and r["service"] == "payment" for r in cs.records)
    rl, _ = vs[("RL", 1)]
    assert not any(r["modality"] == "log" and r["service"] == "payment" for r in rl.records)
    j2, _ = vs[("J2", 1)]
    assert j2.time_tolerance_ns == 2 * SEC


def test_raw_records_are_never_mutated():
    clean = make_records("latency", "payment")
    snapshot = [dict(r) for r in clean]
    for spec, seed in SPECS:
        build(clean, CASE, spec, seed, GT)
    assert clean == snapshot


def test_agent_side_never_imports_evaluation_or_harness():
    res = check_static_imports(__import__("pathlib").Path("."))
    assert res["passed"], res["violations"]
