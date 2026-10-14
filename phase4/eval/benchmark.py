"""Phase 4A timed five-call benchmark (development seed 11 only) -> results/phase4a/five_call_benchmark.json

The five calls are fixed here, before any call is made, from dev-seed-11 cases only (chosen from the Phase 3
dev results; no calibration or test case is opened):
  1 E1  processing_latency:inventory, clean view C0   successful case: E0 top-1 correct on dev, margin 1.0
  2 B4  the same view                                  same evidence, LLM-only
  3 E1  cascading_failure:postgres, C0                 difficult case: E0 top-1 wrong on dev (all views)
  4 B4  service_crash:inventory, MT (traces removed)   missing-telemetry case
  5 B4  healthy control 11:none:-:0, C0, fixed window   intentionally insufficient evidence: no fault exists and
                                                       the detector does not alert; the window is anchored at
                                                       the median record timestamp (label-free) so that the model is
                                                       asked anyway. The correct answer is ABSTAIN.
Ground truth is read only after a call returns, to score it. Docker/Colima must be stopped.

  uv run python -m phase4.eval.benchmark
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from evaluation.freeze import verify_frozen
from evaluation.run_dev import dev_cases
from evaluation.views import build as build_view
from evidencerca import rca_input
from evidencerca.core import detect_alert, make_windows
from evidencerca.evidence import normalize
from phase4.agent import conditions, prompts
from phase4.agent.evidence_pack import median_time
from phase4.agent.llm_client import LLMSettings, OllamaClient

OUT = Path("results/phase4a")
CALLS = [
    {"n": 1, "condition": "E1", "case": "a4f3f9b6", "view": "C0", "role": "successful (E0 correct on dev)"},
    {"n": 2, "condition": "B4", "case": "a4f3f9b6", "view": "C0", "role": "successful case, LLM-only"},
    {"n": 3, "condition": "E1", "case": "ac2a7c4f", "view": "C0", "role": "difficult (cascade; E0 wrong on dev)"},
    {"n": 4, "condition": "B4", "case": "70a3e296", "view": "MT", "role": "missing telemetry (no traces)"},
    {"n": 5, "condition": "B4", "case": "be737f04", "view": "C0", "role": "insufficient evidence (healthy control, "
                                                                          "fixed median-time window)", "fixed_window": True},
]


def sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ollama_rss_mb() -> float:
    tot = 0
    for line in sh("ps", "-axo", "rss,comm").splitlines()[1:]:
        rss, _, comm = line.strip().partition(" ")
        if "ollama" in comm.lower():
            tot += int(rss)
    return round(tot / 1024, 1)


def system_memory() -> dict:
    vm = sh("vm_stat")
    page = 16384
    get = lambda k: int(next((l.split(":")[1].strip().rstrip(".") for l in vm.splitlines() if l.startswith(k)), "0"))
    return {"free_mb": round(get("Pages free") * page / 2**20), "inactive_mb": round(get("Pages inactive") * page / 2**20),
            "compressed_mb": round(get("Pages occupied by compressor") * page / 2**20),
            "swap": sh("sysctl", "-n", "vm.swapusage"),
            "pressure_free_pct": next((l.split(":")[1].strip() for l in sh("memory_pressure").splitlines()
                                       if "free percentage" in l), None)}


class Sampler(threading.Thread):
    def __init__(self, period=0.5):
        super().__init__(daemon=True)
        self.period, self.samples, self.stop_flag = period, [], threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            self.samples.append(ollama_rss_mb())
            time.sleep(self.period)

    def stop(self):
        self.stop_flag.set()
        self.join()
        return max(self.samples) if self.samples else None


def environment() -> dict:
    colima = sh("colima", "status")
    return {"colima_running": "running" in colima.lower() and "not running" not in colima.lower(),
            "docker_processes": sh("pgrep", "-fl", "com.docker|dockerd|limactl|vz"),
            "power": sh("pmset", "-g", "batt").splitlines()[0] if sh("pmset", "-g", "batt") else None,
            "disk_free_gb": round(shutil.disk_usage(".").free / 1e9, 1),
            "ollama_models_dir_mb": round(int((sh("du", "-sk", str(Path.home() / ".ollama/models")) or "0").split()[0]) / 1024, 1),
            "hw": {"model": sh("sysctl", "-n", "hw.model"), "memsize_gb": int(sh("sysctl", "-n", "hw.memsize") or 0) / 2**30,
                   "cpu": sh("sysctl", "-n", "machdep.cpu.brand_string"), "os": sh("sw_vers", "-productVersion")},
            "git_head": sh("git", "rev-parse", "HEAD"), "git_branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD")}


def score_after(call_spec, out, gt) -> dict:
    """Evaluation-side scoring, after the call returned."""
    roots = gt["primary_root_services"]
    d = out.get("diagnosis") or {}
    ranked = [c["service"] for c in d.get("ranked_candidates", [])]
    ver = out.get("verification") or {}
    obs = [c for c in ver.get("claims", []) if c["kind"] == "observed"]
    fails = {}
    for c in obs:
        for k, v in c["checks"].items():
            if v == "fail":
                fails[k] = fails.get(k, 0) + 1
    llm = out.get("llm") or {}
    return {"fault_present": bool(roots), "decision": out.get("decision"),
            "top1_correct": (d.get("suspected_service") in roots) if roots and out.get("decision") == "DIAGNOSE" else None,
            "top3_correct": any(s in roots for s in ranked[:3]) if roots and ranked else None,
            "correct_abstention": (out.get("decision") == "ABSTAIN") if not roots else None,
            "llm_own_suspect_correct": (llm.get("suspected_service") in roots) if roots and llm else None,
            "llm_own_decision": llm.get("decision"),
            "fault_type_correct_llm": (llm.get("suspected_fault_type") == gt["fault_type"]) if llm and roots else None,
            "claims_pre_filter": len((out.get("claims") or {}).get("pre_filter", [])),
            "observed_claims": len(obs), "observed_verified": sum(c["verified"] for c in obs),
            "inferred_claims": sum(c["kind"] == "inferred" for c in ver.get("claims", [])),
            "inferred_premises_valid": sum(c.get("premises_valid", False) for c in ver.get("claims", [])
                                           if c["kind"] == "inferred"),
            "failed_checks_observed": fails,
            "rejected_predicates": sorted(c["predicate"] for c in obs if not c["verified"]),
            "edges": ver.get("summary", {}).get("edges"), "edges_verified": ver.get("summary", {}).get("edges_verified"),
            "verifier_flags": ver.get("flags", [])}


def main() -> int:
    verify_frozen()
    env0 = environment()
    if env0["colima_running"]:
        print("Colima is running: stop it before LLM inference", file=sys.stderr)
        return 2
    settings = LLMSettings()
    client = OllamaClient(settings)
    if client.digest is None:
        print("qwen3:4b not available on the local Ollama server", file=sys.stderr)
        return 2
    client.unload()
    time.sleep(3)
    mem0 = {"system": system_memory(), "ollama_rss_mb_idle": ollama_rss_mb()}
    cases = {c["case_id"][:8]: c for c in dev_cases() if c["role"] == "primary"}
    results, raw_dir = [], OUT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    t_bench = time.time()
    for spec in CALLS:
        case = cases[spec["case"]]
        assert case["gt"]["scenario"]["master_seed"] == 11
        clean = normalize(rca_input.load_case(case["case_id"]))
        view, _ = build_view(clean, case["case_id"], spec["view"], 1, case["gt"])
        win = detect_alert(view)
        alert_fired = win.triggered
        if spec.get("fixed_window"):
            assert not win.triggered, "the insufficient-evidence call expects no alert"
            win = make_windows(view, median_time(view), "fixed-median",
                               reason="label-free: median record timestamp")
        sampler = Sampler()
        sampler.start()
        t0 = time.perf_counter()
        out = conditions.run(spec["condition"], view, win, client)
        wall = time.perf_counter() - t0
        peak = sampler.stop()
        loaded = client.loaded()
        sc = score_after(spec, out, case["gt"])           # ground truth read only now
        call = out.get("call") or {}
        row = {**spec, "case_id": case["case_id"], "view_id": view.view_id, "alert_fired": alert_fired,
               "window_anchor": win.anchor, "end_to_end_wall_s": round(wall, 2),
               "call": {k: call.get(k) for k in ("outcome", "attempts", "done_reason", "prompt_tokens", "output_tokens",
                                                 "load_s", "prompt_eval_s", "eval_s", "total_s", "wall_s",
                                                 "prompt_tokens_per_s", "output_tokens_per_s",
                                                 "context_overflow_risk", "truncated_at_token_limit")},
               "parse": out.get("parse"), "semantic_issues": out.get("semantic_issues"), "citations": out.get("citations"),
               "prompt_meta": out.get("prompt_meta"), "llm": out.get("llm"), "e0": out.get("e0"),
               "e0_abstention": out.get("e0_abstention"), "e1_abstention_descriptive": out.get("e1_abstention_descriptive"),
               "score": sc, "memory": {"ollama_rss_peak_mb": peak, "ollama_ps": [
                   {k: m.get(k) for k in ("name", "digest", "size", "size_vram", "context_length")} for m in loaded],
                   "system_after": system_memory()}}
        results.append(row)
        (raw_dir / f"call{spec['n']}_{spec['condition']}.json").write_text(json.dumps(
            {"spec": spec, "case_id": case["case_id"], "view_id": view.view_id,
             "messages_sha256": (out.get("prompt_meta") or {}).get("case_text_sha256"),
             "call_record": call, "output": {k: v for k, v in out.items() if k != "call"}}, indent=1, default=str) + "\n")
        print(f"call {spec['n']} {spec['condition']} {spec['role']}: outcome={call.get('outcome')} "
              f"parse={(out.get('parse') or {}).get('status')} wall={wall:.1f}s tokens={call.get('prompt_tokens')}/"
              f"{call.get('output_tokens')} decision={out.get('decision')} verified={sc['observed_verified']}/"
              f"{sc['observed_claims']}", flush=True)
    bench_wall = time.time() - t_bench
    client.unload()
    time.sleep(3)
    env1 = environment()
    oks = [r for r in results if r["call"]["outcome"] == "ok"]
    summary = {
        "calls": len(results), "attempts_total": sum(len(r["call"]["attempts"] or []) for r in results),
        "outcomes": {k: sum(r["call"]["outcome"] == k for r in results) for k in ("ok", "timeout", "model_failure")},
        "parse": {k: sum((r["parse"] or {}).get("status") == k for r in results)
                  for k in ("valid", "schema_noncompliant", "malformed_json", "no_response")},
        "json_valid_rate": round(sum((r["parse"] or {}).get("status") in ("valid", "schema_noncompliant") for r in results) / len(results), 3),
        "schema_compliance_rate": round(sum((r["parse"] or {}).get("status") == "valid" for r in results) / len(results), 3),
        "citation_known_alias_rate": round(sum(r["citations"]["n_known_alias"] for r in results if r.get("citations")) /
                                           max(1, sum(r["citations"]["n_cited"] for r in results if r.get("citations"))), 3),
        "observed_claims": sum(r["score"]["observed_claims"] for r in results),
        "observed_verified": sum(r["score"]["observed_verified"] for r in results),
        "model_load_s_cold": results[0]["call"]["load_s"],
        "per_call_wall_s": [r["call"]["wall_s"] for r in results],
        "prompt_tokens": [r["call"]["prompt_tokens"] for r in results],
        "output_tokens": [r["call"]["output_tokens"] for r in results],
        "prompt_tok_per_s": [r["call"]["prompt_tokens_per_s"] for r in results],
        "output_tok_per_s": [r["call"]["output_tokens_per_s"] for r in results],
        "ollama_rss_peak_mb": max((r["memory"]["ollama_rss_peak_mb"] or 0) for r in results),
        "benchmark_wall_s": round(bench_wall, 1), "warm_calls_wall_s_mean": round(
            sum(r["call"]["wall_s"] for r in oks[1:]) / max(1, len(oks[1:])), 2),
    }
    rep = {"schema": "evidencerca.phase4a.five_call_benchmark/v1",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "split": "dev seed 11 only", "prompt_version": prompts.PROMPT_VERSION,
           "model": {"name": settings.model, "digest": client.digest, "ollama_version": client.version,
                     "quantization": "Q4_K_M", "parameters": "4.0B"},
           "settings": settings.__dict__, "environment_before": env0, "environment_after": env1,
           "memory_before": mem0, "summary": summary, "calls": results,
           "cost": {"api_cost_usd": 0.0, "note": "local inference only; no paid API or cloud service"},
           "notes": ["E1's diagnosis-level outcome is E0's by construction; the model's own pick is in llm.*",
                     "abstention scores are descriptive; no tau is applied to B4/E1 in Phase 4A"]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "five_call_benchmark.json").write_text(json.dumps(rep, indent=1, default=str) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
