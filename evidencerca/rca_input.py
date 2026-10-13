"""RCA input builder: the only sanctioned way for agent-side code to read a case.

Guarantees:
  - case IDs must be canonical UUID4 strings, so they cannot carry paths;
  - only files listed in the case's raw-bundle manifest are opened, each resolved
    (following symlinks) and required to lie inside CASES_ROOT/<case_id>; anything
    that resolves elsewhere, including the ground-truth root, raises
    GroundTruthAccessError;
  - the manifest may contain only {schema, case_id, files}; any other key means
    injector metadata leaked into the raw bundle and is rejected;
  - each file's SHA-256 must match the manifest (raw telemetry is immutable).

The builder returns raw records only. Derived views (Phase 3) are built from this
output and stored separately; raw bundles are never written to.
"""

import hashlib
import json
import re
from pathlib import Path

from evidencerca import paths

UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MANIFEST_KEYS = {"schema", "case_id", "files"}
FILE_KEYS = {"name", "bytes", "sha256", "lines"}
ALLOWED_FILES = {"telemetry/traces.jsonl", "telemetry/logs.jsonl", "telemetry/metrics.jsonl"}


class GroundTruthAccessError(PermissionError):
    """Raised for any attempt to read outside the case's raw bundle."""


class BundleIntegrityError(ValueError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _guard(path: Path, case_root: Path) -> Path:
    resolved = path.resolve(strict=True)
    gt_root = paths.GROUND_TRUTH_ROOT.resolve()
    if _inside(resolved, gt_root):
        raise GroundTruthAccessError(f"refusing to read ground truth: {path}")
    if not _inside(resolved, case_root):
        raise GroundTruthAccessError(f"refusing to read outside the case bundle: {path}")
    return resolved


def case_root(case_id: str, cases_root: Path | None = None) -> Path:
    if not isinstance(case_id, str) or not UUID4.match(case_id):
        raise GroundTruthAccessError(f"not a canonical case id: {case_id!r}")
    root = (cases_root or paths.CASES_ROOT).resolve()
    if _inside(root, paths.GROUND_TRUTH_ROOT.resolve()) or _inside(paths.GROUND_TRUTH_ROOT.resolve(), root):
        raise GroundTruthAccessError("cases root overlaps the ground-truth root")
    candidate = root / case_id
    if candidate.is_symlink():
        raise GroundTruthAccessError(f"case directory is a symlink: {case_id}")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != root:
        raise GroundTruthAccessError(f"case directory escapes the cases root: {case_id}")
    return resolved


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(case_id: str, cases_root: Path | None = None) -> dict:
    root = case_root(case_id, cases_root)
    manifest = json.loads(_guard(root / "manifest.json", root).read_text())
    extra = set(manifest) - MANIFEST_KEYS
    if extra:
        raise GroundTruthAccessError(f"raw-bundle manifest carries non-telemetry keys: {sorted(extra)}")
    if manifest.get("case_id") != case_id:
        raise BundleIntegrityError("manifest case_id does not match the directory")
    for f in manifest["files"]:
        if set(f) - FILE_KEYS or f["name"] not in ALLOWED_FILES:
            raise GroundTruthAccessError(f"unexpected manifest file entry: {f}")
    return manifest


def load_case(case_id: str, cases_root: Path | None = None) -> dict:
    """Raw telemetry of one case as parsed OTLP-JSON documents, per signal."""
    root = case_root(case_id, cases_root)
    manifest = read_manifest(case_id, cases_root)
    signals = {}
    for f in manifest["files"]:
        path = _guard(root / f["name"], root)
        if _sha256(path) != f["sha256"]:
            raise BundleIntegrityError(f"{f['name']} does not match its manifest hash")
        signal = Path(f["name"]).stem  # traces | logs | metrics
        signals[signal] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {"case_id": case_id, "manifest_sha256": _sha256(_guard(root / "manifest.json", root)),
            "signals": signals}
