"""Exceptions to the byte-preservation locks, made once when the repository was published (2026-11-01; see
docs/commit_hash_map.md). Every other locked file is still checked byte for byte.

- WITHHELD: sealed audit answer keys, kept private until the human audit is adjudicated. A lock check skips a key
  only when it is absent, so the check still runs wherever the keys are present.
- EDITED_AT_PUBLICATION: files deliberately changed to publish the repository (README status, .gitignore for the
  keys, and the lock tests that gained this exception).
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WITHHELD = frozenset({
    "results/phase4b/audit_key_sealed.json",
    "results/phase4a_v2/audit/audit_key_dev_pilot.SEALED.json",
    "results/phase4a_v3/audit_dry_run/key.SEALED.json",
})
EDITED_AT_PUBLICATION = frozenset({
    ".gitignore",
    "README.md",
    "tests/test_manuscript_prep.py",
    "tests/test_phase4_v2.py",
    "tests/test_phase4_v3.py",
    "tests/test_phase4b.py",
})


def present(rel: str) -> bool:
    """Whether a lock check should compare `rel`: False for a file edited at publication or a withheld key
    that is not on disk."""
    if rel in EDITED_AT_PUBLICATION:
        return False
    return rel not in WITHHELD or (REPO / rel).exists()
