"""Self-test for scripts/validate_telemetry.py.

Runs the validator on the recorded Phase 1 run (results/phase1/) unchanged, which
must pass, and on deliberately corrupted copies, where the targeted checks must
fail. This guards against checks that pass vacuously.
"""

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results" / "phase1"
VALIDATOR = ROOT / "scripts" / "validate_telemetry.py"


@pytest.fixture
def run(tmp_path):
    run_id_file = RESULTS / "RUN_ID"
    if not run_id_file.exists():
        pytest.skip("no recorded Phase 1 run; execute scripts/run_phase1.sh first")
    run_id = run_id_file.read_text().strip()
    with tarfile.open(RESULTS / f"telemetry_{run_id}.tar.gz") as tar:
        tar.extractall(tmp_path, filter="data")
    smoke = tmp_path / "smoke.json"
    smoke.write_text((RESULTS / "smoke_test_results.json").read_text())
    return {"dir": tmp_path / "telemetry", "smoke": smoke,
            "prom": RESULTS / "collector_internal_metrics.prom", "out": tmp_path / "report.json"}


def validate(run) -> dict:
    subprocess.run([sys.executable, VALIDATOR, "--telemetry-dir", run["dir"], "--smoke", run["smoke"],
                    "--collector-metrics", run["prom"], "--out", run["out"]], capture_output=True, check=False)
    report = json.loads(run["out"].read_text())
    return {c["id"]: c["status"] for c in report["checks"]}


def rewrite(path: Path, edit) -> None:
    """Apply `edit` to every parsed line of an OTLP-JSON lines file."""
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    path.write_text("".join(json.dumps(edit(doc)) + "\n" for doc in lines))


def service_of(resource_entry) -> str:
    return next(a["value"]["stringValue"] for a in resource_entry["resource"]["attributes"]
                if a["key"] == "service.name")


def test_recorded_run_passes(run):
    statuses = validate(run)
    assert statuses and all(s == "pass" for s in statuses.values()), statuses


def test_missing_service_spans_detected(run):
    def drop_payment(doc):
        doc["resourceSpans"] = [rs for rs in doc["resourceSpans"] if service_of(rs) != "payment"]
        return doc
    rewrite(run["dir"] / "traces.jsonl", drop_payment)
    statuses = validate(run)
    assert statuses["T1"] == "fail" and statuses["T5"] == "fail"


def test_broken_propagation_detected(run):
    def orphan_order_server_spans(doc):
        for rs in doc["resourceSpans"]:
            if service_of(rs) == "order":
                for ss in rs["scopeSpans"]:
                    for span in ss["spans"]:
                        if span.get("kind") == 2:
                            span["parentSpanId"] = "00000000deadbeef"
        return doc
    rewrite(run["dir"] / "traces.jsonl", orphan_order_server_spans)
    statuses = validate(run)
    assert statuses["T4"] == "fail" and statuses["T5"] == "fail"


def test_uncorrelated_logs_detected(run):
    def strip_context(doc):
        for rl in doc["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for rec in sl["logRecords"]:
                    rec.pop("traceId", None)
                    rec.pop("spanId", None)
        return doc
    rewrite(run["dir"] / "logs.jsonl", strip_context)
    assert validate(run)["L3"] == "fail"


def test_request_count_mismatch_detected(run):
    smoke = json.loads(run["smoke"].read_text())
    smoke["requests"].insert(1, dict(smoke["requests"][0], label="phantom request"))
    run["smoke"].write_text(json.dumps(smoke))
    assert validate(run)["M2"] == "fail"


def test_error_span_detected(run):
    def mark_first_span_error(doc):
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"] = {"code": 2}
        return doc
    rewrite(run["dir"] / "traces.jsonl", mark_first_span_error)
    assert validate(run)["E1"] == "fail"


def test_lost_records_detected(run):
    path = run["dir"] / "metrics.jsonl"
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:-1]))
    assert validate(run)["P2"] == "fail"
