"""Data locations.

data/cases/<case_id>/          raw telemetry bundle (agent-visible, sealed read-only after capture)
data/ground_truth/<case_id>/   injector metadata, validator results, harness records (never agent-visible)
data/views/                    derived evidence views (Phase 3+); raw bundles are never modified

Agent-side code may only read under CASES_ROOT (see evidencerca.rca_input). Both roots
can be relocated with EVR_DATA_ROOT, which keeps them siblings under one data root.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("EVR_DATA_ROOT", REPO_ROOT / "data")).resolve()
CASES_ROOT = DATA_ROOT / "cases"
GROUND_TRUTH_ROOT = DATA_ROOT / "ground_truth"
VIEWS_ROOT = DATA_ROOT / "views"
