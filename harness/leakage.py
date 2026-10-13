"""Label-leakage checks over agent-visible data (harness side: may read ground truth to know what to look for).

Agent-visible = everything evidencerca.rca_input can return: the raw bundle under
data/cases/<case_id>/ (telemetry files + manifest) and the case ID itself.
"""

import ast
import json
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from evidencerca import paths, rca_input
from harness.docker_ops import SERVICES
from harness.faults import BURNER_TOKEN
from harness.schedule import ELIGIBLE

# Vocabulary that would reveal the injected fault or the harness. Matched case-insensitively.
# Plain symptom words ("timeout", "latency", "error") are deliberately NOT forbidden: real systems emit them.
FORBIDDEN = sorted({
    *ELIGIBLE, "none:-", "fault", "inject", "chaos", "netem", "qdisc", "nsenter", "ground_truth", "groundtruth",
    "scenario", "pilot", "mechanism", "harness", "workload", BURNER_TOKEN, "busy-loop", "cpu_pressure",
    "cpu pressure", "crash", "exhaust", "cascad", "primary_root", "root_operation", "fault_type",
    "held_connections", "/delay", "/hold", "/release", "127.0.0.1:9000", "control channel", "lock table",
    "access exclusive", "pg_sleep", "evidencerca-harness", "docker kill",
})
RAW_FILES = {"manifest.json", "telemetry/traces.jsonl", "telemetry/logs.jsonl", "telemetry/metrics.jsonl"}
AGENT_PACKAGES = ["evidencerca", "rca", "baselines"]      # agent-side code (rca/, baselines/ arrive in Phase 3)
ALLOWED_GT_REFERENCES = {"evidencerca/paths.py", "evidencerca/rca_input.py"}  # define/refuse the GT root


# A token must not be preceded by a letter, so "fault" does not match "default" (Postgres init messages,
# httpx's _transports/default.py in tracebacks); suffixes stay matched ("faults", "injected").
_PATTERNS = {tok: re.compile(rb"(?<![a-z])" + re.escape(tok.encode())) for tok in FORBIDDEN}


def scan_tokens(data: bytes) -> dict:
    low = data.lower()
    hits = {tok: len(p.findall(low)) for tok, p in _PATTERNS.items() if tok.encode() in low}
    return {tok: n for tok, n in hits.items() if n}


def _res_attrs(entry) -> dict:
    return {a["key"]: list(a["value"].values())[0] for a in entry.get("resource", {}).get("attributes", [])}


def check_bundle(case_dir: Path, gt: dict) -> dict:
    """Content and structure checks on one sealed raw bundle."""
    files = {str(p.relative_to(case_dir)) for p in case_dir.rglob("*") if p.is_file()}
    hits = {}
    for rel in sorted(files):
        h = scan_tokens((case_dir / rel).read_bytes())
        if h:
            hits[rel] = h
    out = {"case_id": case_dir.name, "files_exact": files == RAW_FILES, "files": sorted(files),
           "forbidden_token_hits": hits}
    tel = case_dir / "telemetry"

    roots, scopes, res_keys, compose_svcs, routes = Counter(), Counter(), {}, set(), set()
    for line in (tel / "traces.jsonl").read_text().splitlines():
        for rs in json.loads(line)["resourceSpans"]:
            ra = _res_attrs(rs)
            res_keys[ra.get("service.name")] = sorted(ra)
            for ss in rs["scopeSpans"]:
                for s in ss["spans"]:
                    if not s.get("parentSpanId"):
                        roots[(ra.get("service.name"), s.get("kind"))] += 1
                    for a in s.get("attributes", []):
                        if a["key"] in ("http.route", "url.full", "url.path", "http.target"):
                            routes.add(list(a["value"].values())[0])
    log_apps = Counter()
    pg_psql_lines = []
    for line in (tel / "logs.jsonl").read_text().splitlines():
        for rl in json.loads(line)["resourceLogs"]:
            ra = _res_attrs(rl)
            for sl in rl["scopeLogs"]:
                scopes[sl.get("scope", {}).get("name", "")] += len(sl["logRecords"])
                if ra.get("service.name") == "postgres":
                    for rec in sl["logRecords"]:
                        body = rec.get("body", {}).get("stringValue", "")
                        m = re.search(r"application_name=(\S+)", body)
                        if m:
                            log_apps[m.group(1)] += 1
                        if "connection authorized" in body and "application_name=psql" in body:
                            pg_psql_lines.append(int(rec.get("timeUnixNano", 0)))
    for line in (tel / "metrics.jsonl").read_text().splitlines():
        for rm in json.loads(line)["resourceMetrics"]:
            ra = _res_attrs(rm)
            if "compose.service" in ra:
                compose_svcs.add(ra["compose.service"])

    t0 = gt["timeline_vm_ns"]["workload_t0_ns"]
    out.update({
        "root_spans": {f"{k[0]}|{k[1]}": v for k, v in roots.items()},
        "only_gateway_server_roots": set(roots) <= {("gateway", 2)},
        "server_routes_or_urls": sorted(routes),
        "no_control_routes": not any(":9000" in r or r in ("/state", "/delay", "/hold", "/release") for r in routes),
        "log_scopes": dict(scopes),
        "observed_containers": sorted(compose_svcs),
        "only_system_under_test_containers": compose_svcs <= set(SERVICES),
        "postgres_application_names": dict(log_apps),
        # psql connections = the image's init scripts (3, at database creation) + the harness's 2 pre-opened
        # sessions. All must precede the workload; the count must be identical across cases (cross-case check).
        "psql_connections": len(pg_psql_lines),
        "psql_connections_all_before_workload_start": all(t <= t0 for t in pg_psql_lines),
        "resource_attribute_keys": res_keys,
    })
    out["passed"] = (out["files_exact"] and not hits and out["only_gateway_server_roots"]
                     and out["no_control_routes"] and out["only_system_under_test_containers"]
                     and out["psql_connections_all_before_workload_start"])
    return out


def check_builder_output(case_id: str, gt: dict, cases_root: Path) -> dict:
    data = rca_input.load_case(case_id, cases_root)
    blob = json.dumps(data).encode()
    hits = scan_tokens(blob)
    return {"case_id": case_id, "top_level_keys": sorted(data), "signals": sorted(data["signals"]),
            "forbidden_token_hits": hits,
            "passed": set(data) == {"case_id", "manifest_sha256", "signals"} and not hits}


def check_path_guard(sample_case: str, cases_root: Path, gt_root: Path) -> list[dict]:
    """Attempts to reach ground truth through the input builder; every one must raise."""
    attempts = [
        ("traversal case id", f"../{gt_root.name}/{sample_case}"),
        ("absolute ground-truth path as case id", str(gt_root / sample_case)),
        ("encoded traversal", "..%2fground_truth%2f" + sample_case),
        ("uppercase / non-canonical id", sample_case.upper()),
        ("empty id", ""),
        ("case id with suffix", sample_case + "/../" + sample_case),
    ]
    results = []
    for label, cid in attempts:
        try:
            rca_input.load_case(cid, cases_root)
            results.append({"attempt": label, "blocked": False})
        except (rca_input.GroundTruthAccessError, FileNotFoundError) as exc:
            results.append({"attempt": label, "blocked": True, "error": type(exc).__name__})

    with tempfile.TemporaryDirectory() as tmp:
        tmp_cases = Path(tmp) / "cases"
        shutil.copytree(cases_root / sample_case, tmp_cases / sample_case)
        for p in [tmp_cases / sample_case, *(tmp_cases / sample_case).rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)
        gt_file = gt_root / sample_case / "ground_truth.json"

        # 1. symlink a telemetry file to the ground-truth file (manifest keeps listing the same name)
        target = tmp_cases / sample_case / "telemetry" / "logs.jsonl"
        target.unlink()
        target.symlink_to(gt_file)
        results.append(_attempt("telemetry file symlinked to ground truth", sample_case, tmp_cases))
        target.unlink()
        shutil.copy(cases_root / sample_case / "telemetry" / "logs.jsonl", target)

        # 2. injector metadata smuggled into the manifest
        man = tmp_cases / sample_case / "manifest.json"
        original = man.read_text()
        man.write_text(json.dumps({**json.loads(original), "fault_type": "x"}))
        results.append(_attempt("manifest with injector metadata key", sample_case, tmp_cases))
        man.write_text(original)

        # 3. extra manifest file entry pointing into ground truth
        doc = json.loads(original)
        doc["files"].append({"name": f"../../ground_truth/{sample_case}/ground_truth.json", "bytes": 0,
                             "sha256": "0", "lines": 0})
        man.write_text(json.dumps(doc))
        results.append(_attempt("manifest entry pointing at ground truth", sample_case, tmp_cases))
        man.write_text(original)

        # 4. the whole case directory replaced by a symlink to the ground-truth directory
        shutil.rmtree(tmp_cases / sample_case)
        (tmp_cases / sample_case).symlink_to(gt_root / sample_case, target_is_directory=True)
        results.append(_attempt("case directory symlinked to ground truth", sample_case, tmp_cases))

    # 5. pointing the builder's cases root at the ground-truth root
    results.append(_attempt("cases root set to the ground-truth root", sample_case, gt_root))
    return results


def _attempt(label: str, case_id: str, cases_root: Path) -> dict:
    try:
        rca_input.load_case(case_id, cases_root)
        return {"attempt": label, "blocked": False}
    except (rca_input.GroundTruthAccessError, rca_input.BundleIntegrityError, FileNotFoundError) as exc:
        return {"attempt": label, "blocked": True, "error": type(exc).__name__}


def check_static_imports(repo: Path) -> dict:
    """Agent-side modules must not import harness code or name the ground-truth location."""
    violations = []
    for pkg in AGENT_PACKAGES:
        for py in sorted((repo / pkg).rglob("*.py")) if (repo / pkg).exists() else []:
            rel = str(py.relative_to(repo))
            src = py.read_text()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom)
                                                            and node.module else [])
                    if any(n.split(".")[0] in ("harness", "scripts", "evaluation") for n in names):
                        violations.append({"file": rel, "issue": f"imports {names}"})
                    if any("GROUND_TRUTH" in n for n in names) and rel not in ALLOWED_GT_REFERENCES:
                        violations.append({"file": rel, "issue": f"imports ground-truth name {names}"})
            if rel not in ALLOWED_GT_REFERENCES and re.search(r"ground_truth|GROUND_TRUTH", src):
                violations.append({"file": rel, "issue": "references ground truth"})
    return {"packages": AGENT_PACKAGES, "violations": violations, "passed": not violations}


def check_mutations(sample_case: str, cases_root: Path) -> list[dict]:
    """Non-vacuity: a planted label token must be caught by the scanner."""
    out = []
    data = (cases_root / sample_case / "telemetry" / "logs.jsonl").read_bytes()
    for token in ["processing_latency", "fault injected", "evr-cpu-burn"]:
        out.append({"planted": token, "detected": bool(scan_tokens(data + token.encode()))})
    return out


def cross_case_consistency(bundles: list[dict]) -> dict:
    """Agent-visible metadata must not differ by scenario: same file set and resource-attribute keys."""
    keys = {json.dumps(b["resource_attribute_keys"], sort_keys=True) for b in bundles}
    files = {tuple(b["files"]) for b in bundles}
    psql = {b["psql_connections"] for b in bundles}
    return {"identical_resource_attribute_keys": len(keys) == 1, "identical_file_sets": len(files) == 1,
            "identical_harness_postgres_footprint": len(psql) == 1, "psql_connections_per_case": sorted(psql),
            "passed": len(keys) == 1 and len(files) == 1 and len(psql) == 1}
