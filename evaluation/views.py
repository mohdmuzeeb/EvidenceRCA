"""Deterministic evidence views (protocol §4) built from the clean normalized records of one case.

Evaluation side: CS, RL and CF need the primary root service(s), so this module may read ground truth.
What reaches a method is only the View: opaque view_id, records, `unavailable` modalities and the time
tolerance. The transform log (what was removed, the decoy service, ...) is returned separately and kept
out of every View. Decoy records have exactly the same fields and id scheme as real records.

Specs (18 views per case): C0; R20/R40/R60 x perturbation seeds 1-3; MT, ML, MM; CS; RL; J1, J2; CF.
"""

import hashlib
import random
from copy import deepcopy

from evidencerca.core import detect_alert, make_windows, observed_graph, robust
from evidencerca.evidence import APP_SERVICES, View, assign_ids, view_id_for

SPECS = ([("C0", 1)] + [(f"R{p}", s) for p in (20, 40, 60) for s in (1, 2, 3)] +
         [("MT", 1), ("ML", 1), ("MM", 1), ("CS", 1), ("RL", 1), ("J1", 1), ("J2", 1), ("CF", 1)])
JITTER_NS = {"J1": 500_000_000, "J2": 2_000_000_000}
DECOY_MEASUREMENTS = ("container_cpu", "process_cpu", "latency_mean")
DECOY_SHIFT_MAD = 4.0


def _u01(*parts) -> float:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _view(case_id, spec, seed, records, unavailable=frozenset(), tol=0):
    return View(view_id_for(case_id, spec, seed), case_id, records, frozenset(unavailable), tol)


def oracle_anchor(gt: dict) -> int:
    """Injection start (or, for controls, the planned onset): the oracle anchor for sensitivity analysis."""
    tl = gt["timeline_vm_ns"]
    return tl["injection_start_ns"] if gt["fault_type"] != "none" else tl["planned_injection_start_ns"]


def build(clean: list[dict], case_id: str, spec: str, seed: int, gt: dict) -> tuple[View, dict]:
    roots = set(gt["primary_root_services"])
    log = {"spec": spec, "perturbation_seed": seed}
    recs = clean
    if spec == "C0":
        return _view(case_id, spec, seed, list(clean)), log
    if spec in ("R20", "R40", "R60"):
        p = int(spec[1:]) / 100
        kept = [r for r in recs if _u01(case_id, spec, seed, r["evidence_id"]) >= p]
        log.update(dropped=len(recs) - len(kept), kept=len(kept))
        return _view(case_id, spec, seed, kept), log
    if spec in ("MT", "ML", "MM"):
        mod = {"MT": "trace", "ML": "log", "MM": "metric"}[spec]
        kept = [r for r in recs if r["modality"] != mod]
        log.update(removed_modality=mod, dropped=len(recs) - len(kept))
        return _view(case_id, spec, seed, kept, {mod}), log
    if spec == "CS":
        # spans of the primary root(s); for postgres (no own spans) its DB statement spans; plus direct children
        drop = {r["span_id"] for r in recs if r["modality"] == "trace" and
                (r["service"] in roots or ("postgres" in roots and r["peer_service"] == "postgres"))}
        drop |= {r["span_id"] for r in recs if r["modality"] == "trace" and r["parent_span_id"] in drop}
        kept = [r for r in recs if not (r["modality"] == "trace" and r["span_id"] in drop)]
        log.update(dropped=len(recs) - len(kept), roots=sorted(roots))
        return _view(case_id, spec, seed, kept), log
    if spec == "RL":
        kept = [r for r in recs if not (r["modality"] == "log" and r["service"] in roots)]
        log.update(dropped=len(recs) - len(kept), roots=sorted(roots))
        return _view(case_id, spec, seed, kept), log
    if spec in JITTER_NS:
        amp = JITTER_NS[spec]
        out = []
        for r in recs:
            x = dict(r)
            d = int((2 * _u01(case_id, spec, seed, r["evidence_id"]) - 1) * amp)
            x["ts_start"] += d
            x["ts_end"] += d
            out.append(x)
        out.sort(key=lambda r: (r["ts_start"], r["modality"], r["service"] or "", r["measurement"]))
        log.update(amplitude_ns=amp)
        return _view(case_id, spec, seed, assign_ids(out, case_id), tol=amp), log
    if spec == "CF":
        clean_view = _view(case_id, "C0", 1, list(clean))
        win = detect_alert(clean_view)
        if not win.triggered:
            win = make_windows(clean_view, oracle_anchor(gt), "oracle")
            log["window_anchor"] = "oracle (clean view did not alert)"
        graph = observed_graph(clean_view)
        adjacent = {x for e in graph for x in e if set(e) & roots}
        cands = sorted(s for s in APP_SERVICES if s not in roots and s not in adjacent) or \
            sorted(s for s in APP_SERVICES if s not in roots)
        decoy = random.Random(f"{case_id}|CF|{seed}").choice(cands)
        (b0, b1), (a0, a1) = win.baseline, win.analysis
        out, shifted = [], 0
        for r in recs:
            x = r
            if r["service"] == decoy and r["measurement"] in DECOY_MEASUREMENTS and a0 <= r["ts_start"] < a1:
                base = [y["value"] for y in recs if y["service"] == decoy and y["measurement"] == r["measurement"]
                        and b0 <= y["ts_start"] < b1]
                if len(base) >= 3:
                    med, mad = robust(base)
                    step = DECOY_SHIFT_MAD * (mad if mad > 0 else max(0.1 * abs(med), 1e-3))
                    x = deepcopy(r)
                    x["value"] = round(r["value"] + step, 6)
                    shifted += 1
            out.append(x)
        log.update(decoy_service=decoy, shifted_records=shifted, window=[a0, a1])
        return _view(case_id, spec, seed, assign_ids([dict(r) for r in out], case_id)), log
    raise ValueError(spec)
