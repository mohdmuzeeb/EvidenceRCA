"""Run one case: fresh testbed -> warm-up -> baseline -> injection -> recovery -> verify -> seal.

Layout (case_id is a random UUID4, assigned here and never derived from the scenario):
  <cases_root>/<case_id>/telemetry/*.jsonl   raw collector output (agent-visible)
  <cases_root>/<case_id>/manifest.json       file hashes only; sealed read-only
  <gt_root>/<case_id>/ground_truth.json      scenario, injector actions, VM-clock timeline, validation
  <gt_root>/<case_id>/harness/               workload records, probes, monitor samples, collector
                                             self-metrics, container stdout
"""

import hashlib
import json
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from harness import docker_ops as d
from harness.faults import Context, fault_for
from harness.monitor import Monitor
from harness.schedule import Scenario, Timeline
from harness.telemetry_check import check_case, load_raw
from harness.validate import capture_continuity, impact_and_recovery, propagated_symptoms, validate_case, windows

SCHEMA = "evidencerca.ground_truth/v1"


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sleep_until_vm(vm_target_ns: int, offset_ns: int) -> None:
    delay = (vm_target_ns - offset_ns - time.time_ns()) / 1e9
    if delay > 0:
        time.sleep(delay)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def seal(raw_dir: Path, case_id: str, read_only: bool = True) -> dict:
    """Write the raw-bundle manifest (hashes only, no scenario information) and make the bundle read-only."""
    tel = raw_dir / "telemetry"
    files = []
    for f in sorted(tel.iterdir()):
        files.append({"name": f"telemetry/{f.name}", "bytes": f.stat().st_size, "sha256": _sha256(f),
                      "lines": sum(1 for _ in open(f, "rb"))})
    manifest = {"schema": "evidencerca.raw_bundle/v1", "case_id": case_id, "files": files}
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if not read_only:
        return {"manifest_sha256": _sha256(raw_dir / "manifest.json"), "files": files}
    ro_file, ro_dir = stat.S_IRUSR | stat.S_IRGRP, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP
    for f in tel.iterdir():
        os.chmod(f, ro_file)
    os.chmod(raw_dir / "manifest.json", ro_file)
    os.chmod(tel, ro_dir)
    os.chmod(raw_dir, ro_dir)
    return {"manifest_sha256": _sha256(raw_dir / "manifest.json"), "files": files}


def ensure_clean() -> None:
    """No project container may be left over from a previous case (also catches unrecovered faults)."""
    left = d.run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={d.PROJECT}").stdout.split()
    if left:
        d.compose("--profile", "harness", "down", "--remove-orphans")


def run_case(scenario: Scenario, timeline: Timeline, cases_root: Path, gt_root: Path, rate: float = 10.0,
             concurrency: int = 32, log=print) -> dict:
    case_id = str(uuid.uuid4())
    raw_dir, gt_dir = cases_root / case_id, gt_root / case_id
    tel_dir, hdir = raw_dir / "telemetry", gt_dir / "harness"
    for p in (tel_dir, hdir):
        p.mkdir(parents=True)
    fault = fault_for(scenario.fault_type, scenario.variant)
    jitter = scenario.onset_jitter_s(timeline)
    workload_s = timeline.warmup_s + timeline.baseline_s + jitter + timeline.injection_s + timeline.recovery_s
    env = {"TELEMETRY_DIR": str(tel_dir), "HARNESS_OUT_DIR": str(hdir)}
    wall0 = time.time()
    gt = {"schema": SCHEMA, "case_id": case_id, "scenario": scenario.as_dict(), "fault_type": scenario.fault_type,
          "target": scenario.target,
          "primary_root_services": [] if scenario.fault_type == "none" else [scenario.target],
          "root_operation": fault.root_operation_for(scenario.target),
          "params": fault.params(scenario.target), "timeline_config": vars(timeline) | {"onset_jitter_s": jitter},
          "workload_config": {"rate_rps": rate, "concurrency": concurrency, "duration_s": workload_s},
          "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    log(f"[{case_id[:8]}] {scenario.key}: start")

    ensure_clean()
    offset_start = d.vm_clock_offset()
    d.compose("up", "-d", "--wait", "--force-recreate", "--renew-anon-volumes", env=env)
    psql_monitor, psql_injector = d.PsqlSession(), d.PsqlSession()  # opened in every case, before warm-up
    monitor = Monitor(hdir / "monitor.jsonl", psql_monitor)
    monitor.start()
    d.compose("--profile", "harness", "run", "-d", "--rm", "--no-deps", "workload",
              "--seed", str(scenario.workload_seed), "--rate", str(rate), "--duration", str(workload_s),
              "--concurrency", str(concurrency), env=env)
    start_file = hdir / "start.json"
    deadline = time.monotonic() + 60
    while not start_file.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    t0 = json.loads(start_file.read_text())["t0_ns"]
    off = offset_start["offset_ns"]

    ctx = Context(target=scenario.target, duration_s=timeline.injection_s, psql_injector=psql_injector)
    warmup_end = t0 + int(timeline.warmup_s * 1e9)
    planned_inject = warmup_end + int((timeline.baseline_s + jitter) * 1e9)
    error = None
    try:
        _sleep_until_vm(planned_inject, off)
        inj = fault.inject(ctx)
        inj_start = inj["vm_ns"] or planned_inject
        log(f"[{case_id[:8]}] injected ({inj['action']})")
        _sleep_until_vm(inj_start + int(timeline.injection_s * 1e9), off)
    except Exception as exc:  # always attempt cleanup
        error = repr(exc)
        inj, inj_start = {"action": "failed", "vm_ns": None, "error": error}, planned_inject
    cleanup = fault.cleanup(ctx)
    inj_end = cleanup["vm_ns"] or (inj_start + int(timeline.injection_s * 1e9))
    log(f"[{case_id[:8]}] cleaned up ({cleanup['action']})")
    recovery_end = inj_end + int(timeline.recovery_s * 1e9)

    summary_file = hdir / "summary.json"
    deadline = time.monotonic() + workload_s + 120
    while not summary_file.exists() and time.monotonic() < deadline:
        time.sleep(1)
    time.sleep(8)  # > one metric export interval + one docker_stats interval
    monitor.stop()
    psql_monitor.close()
    psql_injector.close()

    # Flush order as in Phase 1: services (SDK flush on shutdown), then scrape, then the collector.
    d.compose("stop", "gateway", "order", "payment", "inventory", "postgres", env=env)
    time.sleep(3)
    prom = d.run("curl", "-fsS", "http://127.0.0.1:8888/metrics").stdout
    (hdir / "collector_internal_metrics.prom").write_text(prom)
    stdout_log = d.compose("logs", "--no-color", "--timestamps", env=env, check=False).stdout
    (hdir / "container_stdout.log").write_text(stdout_log)
    d.compose("stop", "otel-collector", env=env)
    d.compose("--profile", "harness", "down", "--remove-orphans", env=env)
    offset_end = d.vm_clock_offset()

    gt["timeline_vm_ns"] = {"workload_t0_ns": t0, "warmup_end_ns": warmup_end, "injection_start_ns": inj_start,
                            "injection_end_ns": inj_end, "recovery_end_ns": recovery_end,
                            "planned_injection_start_ns": planned_inject}
    gt["injection_actions"] = {"inject": inj, "cleanup": cleanup, "error": error}
    gt["clock_offsets"] = {"start": offset_start, "end": offset_end,
                           "note": "offset = VM clock - host clock; all timeline values are VM clock"}

    requests = _jsonl(hdir / "requests.jsonl")
    probes = _jsonl(hdir / "probes.jsonl")
    monitor_recs = _jsonl(hdir / "monitor.jsonl")
    spans, logs_, points, files = load_raw(tel_dir)
    gt["validation"] = validate_case(gt, monitor_recs, probes, requests, spans)
    gt.update(impact_and_recovery(gt, requests, monitor_recs))
    gt["propagation_analysis_only"] = propagated_symptoms(spans, windows(gt["timeline_vm_ns"]),
                                                          gt["primary_root_services"])
    gt["propagated_symptom_services"] = gt["propagation_analysis_only"]["propagated_symptom_services"]
    gt["workload_summary"] = json.loads(summary_file.read_text()) if summary_file.exists() else None
    gt["telemetry_check"] = check_case(gt, tel_dir, prom, requests, stdout_log, spans, logs_, points, files)
    gt["monitor_errors"] = sum(m["kind"] == "error" for m in monitor_recs)  # errors recorded during the case
    gt["capture_continuity"] = capture_continuity(monitor_recs, gt["workload_summary"], gt["workload_config"]["duration_s"])
    gt["raw_bundle"] = seal(raw_dir, case_id)
    gt["wall_clock_s"] = round(time.time() - wall0, 1)
    ok_fault = gt["validation"]["fault_occurred"] in (True, None) and all(
        c["passed"] for c in gt["validation"]["checks"])
    gt["case_valid"] = bool(ok_fault and gt["recovery"]["recovered"] and gt["telemetry_check"]["passed"]
                            and error is None and gt["monitor_errors"] == 0 and gt["capture_continuity"]["passed"])
    (gt_dir / "ground_truth.json").write_text(json.dumps(gt, indent=2, default=str) + "\n")
    log(f"[{case_id[:8]}] done in {gt['wall_clock_s']} s: valid={gt['case_valid']} "
        f"occurred={gt['validation']['fault_occurred']} recovered={gt['recovery']['recovered']} "
        f"telemetry={gt['telemetry_check']['passed']}")
    return gt
