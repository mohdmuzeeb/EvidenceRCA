"""Leakage tests: the RCA input builder can reach raw telemetry only, never ground truth.

Synthetic bundles make these tests independent of Docker; the last tests also
run over real captured cases when they exist (data/cases).
"""

import json
import shutil
import uuid
from pathlib import Path

import pytest

from evidencerca import paths, rca_input
from harness import leakage
from harness.case import seal

REPO = Path(__file__).resolve().parent.parent

CLEAN_SPAN = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "gateway"}}]},
                                 "scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "kind": 2,
                                                            "name": "GET /api/stock/{item_id}"}]}]}]}
CLEAN_LOG = {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "order"}}]},
                               "scopeLogs": [{"logRecords": [{"body": {"stringValue": "request completed"}}]}]}]}
CLEAN_METRIC = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": []}]}


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A data root with one sealed (writable) case and its ground truth."""
    cases, gt = tmp_path / "cases", tmp_path / "ground_truth"
    monkeypatch.setattr(paths, "CASES_ROOT", cases)
    monkeypatch.setattr(paths, "GROUND_TRUTH_ROOT", gt)
    case_id = str(uuid.uuid4())
    tel = cases / case_id / "telemetry"
    tel.mkdir(parents=True)
    (tel / "traces.jsonl").write_text(json.dumps(CLEAN_SPAN) + "\n")
    (tel / "logs.jsonl").write_text(json.dumps(CLEAN_LOG) + "\n")
    (tel / "metrics.jsonl").write_text(json.dumps(CLEAN_METRIC) + "\n")
    seal(cases / case_id, case_id, read_only=False)
    (gt / case_id).mkdir(parents=True)
    (gt / case_id / "ground_truth.json").write_text(json.dumps({"fault_type": "service_crash", "target": "order"}))
    return {"cases": cases, "gt": gt, "case_id": case_id}


def blocked(case_id, cases_root=None):
    with pytest.raises((rca_input.GroundTruthAccessError, rca_input.BundleIntegrityError, FileNotFoundError)):
        rca_input.load_case(case_id, cases_root)
    return True


def test_builder_reads_raw_bundle(data_root):
    data = rca_input.load_case(data_root["case_id"])
    assert set(data) == {"case_id", "manifest_sha256", "signals"}
    assert set(data["signals"]) == {"traces", "logs", "metrics"}
    assert not leakage.scan_tokens(json.dumps(data).encode())


@pytest.mark.parametrize("make_id", [
    lambda c, g: f"../ground_truth/{c}",
    lambda c, g: str(g / c),
    lambda c, g: f"..%2fground_truth%2f{c}",
    lambda c, g: c.upper(),
    lambda c, g: "",
    lambda c, g: f"{c}/../{c}",
    lambda c, g: str(uuid.uuid4()),          # well-formed but nonexistent
])
def test_traversal_and_malformed_ids_blocked(data_root, make_id):
    assert blocked(make_id(data_root["case_id"], data_root["gt"]))


def test_symlinked_telemetry_file_to_ground_truth_blocked(data_root):
    cid = data_root["case_id"]
    f = data_root["cases"] / cid / "telemetry" / "logs.jsonl"
    f.unlink()
    f.symlink_to(data_root["gt"] / cid / "ground_truth.json")
    assert blocked(cid)


def test_symlinked_case_dir_blocked(data_root):
    cid = data_root["case_id"]
    shutil.rmtree(data_root["cases"] / cid)
    (data_root["cases"] / cid).symlink_to(data_root["gt"] / cid, target_is_directory=True)
    assert blocked(cid)


def test_manifest_with_injector_metadata_blocked(data_root):
    cid = data_root["case_id"]
    man = data_root["cases"] / cid / "manifest.json"
    man.write_text(json.dumps({**json.loads(man.read_text()), "fault_type": "service_crash"}))
    assert blocked(cid)


def test_manifest_entry_into_ground_truth_blocked(data_root):
    cid = data_root["case_id"]
    man = data_root["cases"] / cid / "manifest.json"
    doc = json.loads(man.read_text())
    doc["files"].append({"name": f"../../ground_truth/{cid}/ground_truth.json", "bytes": 1, "sha256": "x",
                         "lines": 1})
    man.write_text(json.dumps(doc))
    assert blocked(cid)


def test_tampered_raw_file_detected(data_root):
    cid = data_root["case_id"]
    with open(data_root["cases"] / cid / "telemetry" / "logs.jsonl", "a") as f:
        f.write(json.dumps(CLEAN_LOG) + "\n")
    assert blocked(cid)


def test_cases_root_pointing_at_ground_truth_blocked(data_root):
    assert blocked(data_root["case_id"], data_root["gt"])


def test_agent_side_code_never_references_ground_truth():
    result = leakage.check_static_imports(REPO)
    assert result["passed"], result["violations"]


def test_static_check_flags_a_violating_module(tmp_path):
    pkg = tmp_path / "rca"
    pkg.mkdir()
    (pkg / "bad.py").write_text("from harness.faults import FAULTS\nPATH = 'data/ground_truth'\n")
    issues = leakage.check_static_imports(tmp_path)["violations"]
    assert {i["issue"] for i in issues} >= {"imports ['FAULTS', 'harness.faults']", "references ground truth"}


@pytest.mark.parametrize("token", ["processing_latency", "service_crash", "db_pool_exhaustion", "network_timeout",
                                   "cpu_pressure", "cascading_failure", "Fault injected", "evr-cpu-burn",
                                   "LOCK TABLE items", "netem delay"])
def test_scanner_detects_label_tokens(token):
    assert leakage.scan_tokens(json.dumps(CLEAN_LOG).encode() + token.encode())


def test_scanner_clean_on_symptom_vocabulary():
    symptoms = (b"ReadTimeout downstream unavailable request latency QueuePool limit reached connection refused 503 "
                b"The default database encoding httpx/_transports/default.py")
    assert not leakage.scan_tokens(symptoms)


def real_cases():
    if not paths.CASES_ROOT.exists():
        return []
    return sorted(p.name for p in paths.CASES_ROOT.iterdir() if p.is_dir())


@pytest.mark.parametrize("case_id", real_cases() or [pytest.param(None, marks=pytest.mark.skip("no captured cases"))])
def test_captured_bundle_has_no_leakage(case_id):
    gt = json.loads((paths.GROUND_TRUTH_ROOT / case_id / "ground_truth.json").read_text())
    result = leakage.check_bundle(paths.CASES_ROOT / case_id, gt)
    assert result["passed"], {k: v for k, v in result.items() if k not in ("resource_attribute_keys",)}
    assert leakage.check_builder_output(case_id, gt, paths.CASES_ROOT)["passed"]
