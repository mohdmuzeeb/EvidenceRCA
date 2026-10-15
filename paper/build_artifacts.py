"""Build IEEE-ready tables, figures and number macros from RECORDED results only.

Nothing here runs an experiment, calls a model or changes a result file. Every number is read from:
  results/final/test_results.json, results/final/test_trials.jsonl.gz       (confirmatory, frozen deterministic)
  results/phase4b/condition_comparison.json, claim_metrics.json,
  runtime_summary.json, llm_trials.jsonl.gz                                   (exploratory, Phase 4B)
Derived quantities (per-fault-type counts of stored Phase 4B trials, risk-coverage curves of stored frozen trials,
Wilson intervals of recorded counts) are recomputed with the frozen helpers and asserted against the recorded totals.

Run:  uv run python -m paper.build_artifacts     (writes paper/tables, paper/figures, paper/numbers.tex,
                                                    paper/artifact_manifest.json; PDFs/PNGs via rsvg-convert)
"""
from __future__ import annotations

import gzip
import hashlib
from decimal import ROUND_HALF_UP, Decimal
import json
import os
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from evaluation.run_final import SCORES, rc, wilson   # frozen statistics helpers (unchanged)
from paper.svgfig import COLOR, MARKER, Axes, Fig, legend

REPO = Path(__file__).resolve().parents[1]
PAPER = REPO / "paper"
TABLES, FIGURES = PAPER / "tables", PAPER / "figures"
INPUTS = {
    "final_test_results": "results/final/test_results.json",
    "final_test_trials": "results/final/test_trials.jsonl.gz",
    "p4b_condition_comparison": "results/phase4b/condition_comparison.json",
    "p4b_claim_metrics": "results/phase4b/claim_metrics.json",
    "p4b_runtime_summary": "results/phase4b/runtime_summary.json",
    "p4b_llm_trials": "results/phase4b/llm_trials.jsonl.gz",
}
VIEWS_ALL = ["C0", "R20", "R40", "R60", "MT", "ML", "MM", "CS", "RL", "J1", "J2", "CF"]
VIEWS_P4B = ["C0", "R40", "MT", "ML", "CF"]
VIEW_NAME = {"C0": "C0 clean", "R20": "R20 random loss 20%", "R40": "R40 random loss 40%",
             "R60": "R60 random loss 60%", "MT": "MT no traces", "ML": "ML no logs", "MM": "MM no metrics",
             "CS": "CS root spans removed", "RL": "RL root logs removed", "J1": "J1 clock jitter",
             "J2": "J2 clock jitter (strong)", "CF": "CF conflicting decoy"}
METHODS = ["B1", "B2", "B3", "E0-A-graph", "E0"]
FAULTS = ["processing_latency", "service_crash", "cpu_pressure", "network_timeout", "db_pool_exhaustion",
          "cascading_failure"]
FAULT_NAME = {"processing_latency": "Processing latency", "service_crash": "Service crash",
              "cpu_pressure": "CPU pressure (strong)", "network_timeout": "Network timeout",
              "db_pool_exhaustion": "DB pool exhaustion", "cascading_failure": "Cascading failure"}
CONFIRMATORY = "Confirmatory (frozen protocol, held-out test seeds 41/53/67)"
EXPLORATORY = "Exploratory (Phase 4B, post-study; held-out seeds previously inspected for E0)"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load():
    j = lambda k: json.loads((REPO / INPUTS[k]).read_text())
    trials = [json.loads(l) for l in gzip.open(REPO / INPUTS["final_test_trials"], "rt")]
    p4b = [json.loads(l) for l in gzip.open(REPO / INPUTS["p4b_llm_trials"], "rt") if '"type": "trial"' in l]
    return {"final": j("final_test_results"), "trials": trials, "cc": j("p4b_condition_comparison"),
            "cm": j("p4b_claim_metrics"), "rt": j("p4b_runtime_summary"), "p4b_trials": p4b}


# ---------------------------------------------------------------- formatting
def rnd(x, d=2, sign=False, lead=True) -> str:
    """Round half-up from the recorded decimal (as the frozen reports did: 0.2135 -> 0.214); a typographic minus;
    no '-0.00'; optional explicit '+'; lead=False drops the leading zero (.81) for dense tables."""
    q = Decimal(str(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    s = f"{abs(q):.{d}f}"
    if not lead and s.startswith("0."):
        s = s[1:]
    return ("−" if q < 0 else "+" if sign and q > 0 else "") + s


def r2(x):
    return "--" if x is None else rnd(x)


def ci(c, d=2, lead=True, sep=", "):
    return "--" if not c else f"[{rnd(c[0], d, lead=lead)}{sep}{rnd(c[1], d, lead=lead)}]"


def ci_pct(c, d=1):
    return "--" if not c else f"[{rnd(100 * c[0], d)}, {rnd(100 * c[1], d)}]"


def two_line(v):
    return f"{v}\n{VIEW_NAME[v].split(' ', 1)[1]}"


def kn(k, n):
    return f"{k}/{n}"


def pct(x, d=1):
    return f"{rnd(100 * x, d)}%"


def pval(p):
    if p is None:
        return "--"
    if p == 0 or p < 1e-6:
        return "<1e-6"
    return f"{p:.2g}" if p < 0.01 else f"{p:.2f}"


def tex(s: str) -> str:
    out = s.replace("\\", "\\textbackslash{}").replace("$", "\\$")
    for a, b in (("&", "\\&"), ("%", "\\%"), ("#", "\\#"), ("_", "\\_"), ("<=", "$\\le$"), ("<1e-6", "$<$1e-6"),
                 ("->", "$\\to$"), ("~", "$\\sim$"), ("--", "--")):
        out = out.replace(a, b)
    out = out.replace("†", "$^\\dagger$").replace("‡", "$^\\ddagger$").replace("×", "$\\times$")
    out = out.replace("−", "$-$").replace("≈", "$\\approx$").replace("τ", "$\\tau$")
    return out


class Table:
    def __init__(self, tid, tier, title, header, rows, notes, wide=False, align=None):
        self.tid, self.tier, self.title, self.header, self.rows = tid, tier, title, header, rows
        self.notes, self.wide = notes, wide
        self.align = align or ("l" + "r" * (len(header) - 1))

    @staticmethod
    def _head(h: str) -> str:
        if "\n" not in h:
            return f"\\textbf{{{tex(h)}}}"
        inner = " \\\\ ".join(f"\\textbf{{{tex(x)}}}" for x in h.split("\n"))
        return f"\\begin{{tabular}}[b]{{@{{}}c@{{}}}}{inner}\\end{{tabular}}"

    def latex(self) -> str:
        env = "table*" if self.wide else "table"
        lines = [f"% Auto-generated by paper/build_artifacts.py from recorded results; do not edit by hand.",
                 f"\\begin{{{env}}}[t]", "\\centering",
                 f"\\caption{{{tex(self.tier)}: {tex(self.title)}}}", f"\\label{{tab:{self.tid}}}",
                 "\\footnotesize", "\\setlength{\\tabcolsep}{3.5pt}", f"\\begin{{tabular}}{{{self.align}}}",
                 "\\toprule", " & ".join(self._head(h) for h in self.header) + " \\\\", "\\midrule"]
        for r in self.rows:
            if r == "MIDRULE":
                lines.append("\\midrule")
            else:
                lines.append(" & ".join(tex(str(c)) for c in r) + " \\\\")
        lines += ["\\bottomrule", "\\end{tabular}"]
        if self.notes:
            lines += ["\\\\[3pt]", "\\begin{minipage}{\\linewidth}\\scriptsize",
                      " ".join(tex(n) for n in self.notes), "\\end{minipage}"]
        lines.append(f"\\end{{{env}}}")
        return "\n".join(lines) + "\n"

    def markdown(self) -> str:
        out = [f"### Table `{self.tid}`: {self.title}", "", f"*{self.tier}.*", "",
               "| " + " | ".join(h.replace("\n", " ") for h in self.header) + " |", "|" + "---|" * len(self.header)]
        for r in self.rows:
            if r != "MIDRULE":
                out.append("| " + " | ".join(str(c) for c in r) + " |")
        if self.notes:
            out += [""] + [f"- {n}" for n in self.notes]
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------- confirmatory tables
def acc_row(final, method, view):
    rows = [r for r in final["accuracy"] if r["method"] == method and r["condition"] == view
            and r["anchor"] == "alert"]
    assert len(rows) == 1, (method, view)
    return rows[0]


def t_conf_localization(D):
    F = D["final"]
    rows = []
    for v in VIEWS_ALL:
        e0 = acc_row(F, "E0", v)
        cells = [v, str(e0["n_fault_trials"])]
        for m in METHODS:
            r = acc_row(F, m, v)
            cells.append(f"{rnd(r['top1_correct_rate_all_faults'], lead=False)} "
                         f"{ci(r['top1_correct_rate_all_faults_ci'], lead=False, sep=',')}")
        cells.append(kn(e0["top1_correct_of_triggered"], e0["n_fault_trials"]))
        cells.append(rnd(e0["top3_correct_rate_all_faults"], lead=False))
        rows.append(cells)
    return Table("conf_localization", CONFIRMATORY,
                 "Top-1 root-service localization by telemetry view, rate [95% CI] over all fault trials "
                 "(detection failures count as wrong).",
                 ["View", "n", "B1", "B2", "B3", "E0-A-graph", "E0", "E0\nk/n", "E0\ntop-3"], rows,
                 ["Views: " + "; ".join(VIEW_NAME[v] for v in VIEWS_ALL) + ".",
                  "47 base fault cases (6 healthy controls reported separately). n = fault trials: one per case, "
                  "or three perturbation seeds per case for R20/R40/R60.",
                  "95% CI: Wilson over cases; for R20/R40/R60 the per-case mean over 3 seeds with a cluster "
                  "bootstrap over cases (10,000 resamples).",
                  "MT: the SLO detector alerted on 45/47 fault cases; the 2 undetected count as wrong.",
                  "B2 and B3 are re-implementations inspired by published methods, not validated against the "
                  "authors' code."], wide=True)


def t_conf_fault_types(D):
    F = D["final"]["per_fault_type_C0"]
    rows = []
    for ft in FAULTS:
        n = F[ft]["E0"]["n"]
        rows.append([FAULT_NAME[ft], str(n)] + [kn(F[ft][m]["top1"], n) for m in METHODS]
                    + [kn(F[ft]["E0"]["top3"], n)])
    tot = {m: sum(F[ft][m]["top1"] for ft in FAULTS) for m in METHODS}
    rows += ["MIDRULE", ["All faults", "47"] + [kn(tot[m], 47) for m in METHODS]
             + [kn(sum(F[ft]["E0"]["top3"] for ft in FAULTS), 47)]]
    assert tot["E0"] == acc_row(D["final"], "E0", "C0")["top1_correct_of_triggered"]
    return Table("conf_fault_types", CONFIRMATORY,
                 "Top-1 localization by fault type on the clean view C0 (k/n base cases).",
                 ["Fault type", "n", "B1", "B2", "B3", "E0-A-\ngraph", "E0", "E0\ntop-3"], rows,
                 ["Failed categories are kept: no method localizes cascading failures (E0 not even in its top-3), "
                  "and E0 localizes 3/9 strong CPU-pressure faults, often blaming downstream database/pool symptoms.",
                  "Per-type counts are descriptive (n = 3 to 12)."])


def t_conf_paired(D):
    PC = D["final"]["paired_comparisons"]
    P = PC["C0"]
    rows = []
    for comp in ("B1", "B2", "B3", "E0-A-graph"):
        p = P[f"E0_vs_{comp}"]
        rows.append([f"E0 vs {comp}", rnd(p["mean_diff_top1"], sign=True), ci(p["ci_cluster_bootstrap"]),
                     f"{p['discordant_e0_only']} / {p['discordant_comp_only']}", pval(p["holm_p"])])
    r40, mt = PC["R40"]["E0_vs_B2"], PC["MT"]["E0_vs_B1"]
    return Table("conf_paired", CONFIRMATORY,
                 "Paired top-1 comparisons on C0 (n = 47 base cases).",
                 ["Comparison", "Diff.", "95% CI", "Discordant\nE0 / other", "Holm p"], rows,
                 ["Difference of per-case top-1 means; cluster bootstrap over cases (10,000 resamples); exact "
                  "McNemar test with Holm correction over the 4 comparisons.",
                  f"No advantage elsewhere: R40, E0 vs B2 = {rnd(r40['mean_diff_top1'], sign=True)} "
                  f"{ci(r40['ci_cluster_bootstrap'])}; MT, E0 vs B1 = {rnd(mt['mean_diff_top1'], sign=True)} "
                  f"{ci(mt['ci_cluster_bootstrap'])} (bootstrap CI, no multiplicity correction across views)."],
                 align="lrrrr")


SCORE_NAME = {"E0:S": "E0 S (pre-registered)", "E0:A-margin": "E0 margin only (A-margin)",
              "E0:A-ver": "E0 S w/o verifier coverage", "E0-A-graph:S": "E0-A-graph S",
              "B1:margin": "B1 margin", "B2:margin": "B2 margin", "B3:margin": "B3 margin"}


def t_conf_abstention(D):
    A = D["final"]["abstention"]
    rows = []
    for s in SCORE_NAME:
        a = A[s]

        def op(o):
            if o.get("tau") is None:
                return ["n/a", "", ""]
            risk = "--" if o["selective_risk"] is None else \
                f"{rnd(o['selective_risk'], 3, lead=False)} {ci(o['selective_risk_ci'], 3, lead=False, sep=',')}"
            return [rnd(o["tau"], 3, lead=False), rnd(o["coverage"], 3, lead=False), risk]
        assert a["op1"].get("controls_diagnosed", 0) == 0
        rows.append([SCORE_NAME[s], str(a["n_diagnosed_fault_trials"]),
                     f"{rnd(a['aurc'], 3, lead=False)} {ci(a['aurc_ci'], 3, lead=False, sep=',')}"]
                    + op(a["op1"]) + op(a["op2"]))
    s_, m_ = A["E0:S"], A["E0:A-margin"]
    return Table("conf_abstention", CONFIRMATORY,
                 "Selective prediction (abstention) on diagnosed fault trials, all views pooled; thresholds fixed "
                 "on the calibration seeds 23/37.",
                 ["Score", "n", "AURC [95% CI]", "op1\nτ", "op1\ncov.", "op1 risk\n[95% CI]", "op2\nτ", "op2\ncov.",
                  "op2 risk\n[95% CI]"], rows,
                 ["AURC: lower is better; cluster bootstrap over cases. op1: minimum risk with coverage >= 0.7; "
                  "op2: maximum coverage with risk <= 0.1 (both chosen on calibration); n/a: not achievable on "
                  "calibration. Risk CI: Wilson.",
                  f"No healthy-control trial was diagnosed at any operating point (0/{s_['op1']['control_trials']}).",
                  f"Negative finding: the pre-registered composite S is worse than its margin component alone "
                  f"(AURC {rnd(s_['aurc'], 3)} vs {rnd(m_['aurc'], 3)}, overlapping CIs), and removing the "
                  "verifier-coverage signal does not hurt.",
                  f"Scores are heuristic orderings, not probabilities. Without abstention E0's selective risk is "
                  f"{rnd(s_['A-abst_selective_risk_at_full_coverage'], 3)}."],
                 wide=True, align="lrrrrrrrr")


def t_conf_verifier(D):
    V = D["final"]["verification"]["by_condition"]
    rows = []
    for v in VIEWS_ALL:
        x = V[v]
        rows.append([v, str(x["diagnoses"]), kn(x["verified"], x["observed"]),
                     f"{pct(x['verified_rate'])} {ci_pct(wilson(x['verified'], x['observed']))}",
                     rnd(x["verified_per_diagnose"], 1), kn(x["edges_verified"], x["edges"])])
    rej = D["final"]["verification"]["rejections"]
    onset = sum(v for k, v in rej.items() if k.startswith("onset_before"))
    return Table("conf_verifier", CONFIRMATORY,
                 "Deterministic verifier acceptance of E0's template claims (alert-anchored DIAGNOSE outputs).",
                 ["View", "Diag.", "Verified /\nobserved", "Rate\n[Wilson 95%]", "Verified\nper diag.",
                  "Edges\nverified"], rows,
                 ["Acceptance by the frozen verifier (checks V0-V6), not independently established correctness: "
                  "the blinded human audit is pending.",
                  f"{onset} of {sum(rej.values())} rejections are onset_before claims (E0 cites a caller's span as "
                  "the object's first anomaly), a known E0 limitation; rejected claims are reported, never "
                  "rewritten."], align="lrrrrr")


# ---------------------------------------------------------------- exploratory tables
def t_exp_diagnosis(D):
    Dg = D["cc"]["diagnosis"]
    names = [("B4", "B4 (LLM only)"), ("E1", "E1 (E0 loc. + LLM)†"), ("E1-frozen-gated", "E1 frozen-gated (= E0)"),
             ("E2", "E2 (E1 + ownership)"), ("B4-G", "B4-G (gate + B4)‡")]
    rows = []
    for c, label in names:
        a = Dg[c]["all"]
        assert a["top1"]["n"] == a["top3"]["n"] == a["fault_type"]["n"] == a["fault_abstained"]["n"] == 235
        assert a["control_false_diagnoses"]["n"] == 30
        rows.append([label, str(a["calls"]), f"{a['top1']['k']} ({rnd(a['top1']['rate'])})",
                     ci(a["top1_case_mean_cluster_boot95"]), str(a["top3"]["k"]), str(a["fault_type"]["k"]),
                     str(a["wrong_service_diagnoses"]), str(a["fault_abstained"]["k"]),
                     f"{a['control_false_diagnoses']['k']} {ci(a['control_false_diagnoses']['wilson95'])}"])
    p = D["cc"]["paired"]["E1_vs_B4_top1"]
    return Table("exp_diagnosis", EXPLORATORY,
                 "Diagnosis-level outcomes with a local LLM (qwen3:4b, prompt p4a-v1): 235 fault views "
                 "(47 cases × 5 views) and 30 control views (6 controls × 5 views).",
                 ["Condition", "LLM\ncalls", "Top-1 /235\n(rate)", "Case-mean\n95% CI", "Top-3\n/235",
                  "Fault type\n/235", "Wrong\nservice", "Abstained\n/235", "Controls diagnosed\n/30 [Wilson]"],
                 rows,
                 ["† E1's localization is E0's by design and its LLM is shown E0's ranking (B4's is not), so E1's top-1 "
                  "is the frozen E0 result, not an LLM gain. ‡ B4-G is a hybrid, not an LLM-only baseline.",
                  f"E1 - B4 per-case top-1 difference {rnd(p['mean_case_difference_a_minus_b'], sign=True)} "
                  f"{ci(p['cluster_boot95'])} (cluster bootstrap over 47 cases). E1 is read under B4's window rule "
                  "(alert window, else a median-time window), so it diagnoses on non-alerted controls; the frozen "
                  "gated reading abstains there.",
                  "E2's diagnosis equals E1's by construction: E2 does not change localization."],
                 wide=True, align="lrrrrrrrr")


def t_exp_views(D):
    Dg, P = D["cc"]["diagnosis"], D["cc"]["paired"]
    top, ft = P["E1_vs_B4_top1"]["per_spec"], P["E1_vs_B4_fault_type"]["per_spec"]
    for v in VIEWS_P4B:
        assert top[v]["n_base_cases"] == ft[v]["n_base_cases"] == 47
    rows = [["B4 top-1 (of 47)"] + [str(top[v]["b_correct"]) for v in VIEWS_P4B],
            ["E1 (= E0) top-1 (of 47)"] + [str(top[v]["a_correct"]) for v in VIEWS_P4B],
            ["Discordant: E1 only / B4 only"] + [f"{top[v]['a_only']} / {top[v]['b_only']}" for v in VIEWS_P4B],
            ["Holm-corrected McNemar p"] + [pval(top[v]["holm_p"]) for v in VIEWS_P4B], "MIDRULE",
            ["B4 fault type (of 47)"] + [str(ft[v]["b_correct"]) for v in VIEWS_P4B],
            ["E1 fault type (of 47)"] + [str(ft[v]["a_correct"]) for v in VIEWS_P4B],
            ["Holm-corrected McNemar p"] + [pval(ft[v]["holm_p"]) for v in VIEWS_P4B], "MIDRULE",
            ["B4 controls diagnosed (of 6)"] + [str(Dg["B4"]["by_spec"][v]["control_false_diagnoses"]["k"])
                                                for v in VIEWS_P4B],
            ["E1 controls diagnosed (of 6)"] + [str(Dg["E1"]["by_spec"][v]["control_false_diagnoses"]["k"])
                                                for v in VIEWS_P4B]]
    return Table("exp_views", EXPLORATORY, "B4 versus E1 by telemetry view (one view per base case).",
                 ["", *VIEWS_P4B], rows,
                 ["C0 clean; R40 40% random record loss (perturbation seed 1); MT no traces; ML no logs; CF "
                  "conflicting decoy. Exact McNemar tests, Holm correction over the 5 views.",
                  "Under R40 and MT the top-1 difference is not significant; under MT both localize 15/47 with 10 "
                  "discordant cases each way."], align="lrrrrr")


def t_exp_fault_types(D):
    T = [t for t in D["p4b_trials"] if t["fault_present"]]
    agg = defaultdict(Counter)
    for t in T:
        if t["condition"] in ("B4", "E1"):
            a = agg[(t["condition"], t["fault_type"])]
            a["n"] += 1
            a["top1"] += bool(t["top1_correct"])
            a["ft"] += bool(t["fault_type_correct"])
    for c in ("B4", "E1"):   # must reproduce the recorded totals
        assert sum(agg[(c, f)]["top1"] for f in FAULTS) == D["cc"]["diagnosis"][c]["all"]["top1"]["k"]
        assert sum(agg[(c, f)]["ft"] for f in FAULTS) == D["cc"]["diagnosis"][c]["all"]["fault_type"]["k"]
    rows = [[FAULT_NAME[f], str(agg[("B4", f)]["n"]), kn(agg[("B4", f)]["top1"], agg[("B4", f)]["n"]),
             kn(agg[("E1", f)]["top1"], agg[("E1", f)]["n"]), kn(agg[("B4", f)]["ft"], agg[("B4", f)]["n"]),
             kn(agg[("E1", f)]["ft"], agg[("E1", f)]["n"])] for f in FAULTS]
    rows += ["MIDRULE", ["All faults", "235"] + [kn(sum(agg[(c, f)][m] for f in FAULTS), 235)
                                               for m, c in (("top1", "B4"), ("top1", "E1"), ("ft", "B4"),
                                                            ("ft", "E1"))]]
    wrong = Counter(t["suspected_service"] for t in T if t["condition"] == "B4" and t["decision"] == "DIAGNOSE"
                    and not t["top1_correct"])
    assert sum(wrong.values()) == D["cc"]["diagnosis"]["B4"]["all"]["wrong_service_diagnoses"]
    top_wrong = ", ".join(f"{s} {k}" for s, k in wrong.most_common(2))
    return Table("exp_fault_types", EXPLORATORY,
                 "Top-1 and fault-type accuracy by fault type, pooled over the 5 views (k/n fault views).",
                 ["Fault type", "Views", "B4\ntop-1", "E1 (= E0)\ntop-1", "B4\nfault type", "E1\nfault type"], rows,
                 ["Counted from the stored per-view trial records (results/phase4b/llm_trials.jsonl.gz); totals "
                  "equal the recorded summary.",
                  f"B4's wrong diagnoses concentrate on a few services ({top_wrong} of "
                  f"{sum(wrong.values())}). Neither condition localizes a cascading failure, and neither types a "
                  "CPU-pressure fault correctly; B4 localizes strong CPU pressure slightly more often than E0."],
                 align="lrrrrr")


def t_exp_claims(D):
    C = D["cm"]["conditions"]
    lab = [("B4", "B4 (LLM)"), ("E1", "E1 (LLM)"), ("E2-canonical", "E2 canonical§"), ("E0", "E0 templates")]
    rows = []
    for c, name in lab:
        a = C[c]["all"]
        e = a["propagation_edges"]
        rows.append([name, str(a["calls_with_claims_basis"]), str(a["claims_total"]),
                     kn(a["observed_verified"], a["observed"]),
                     f"{pct(a['observed_verified_rate']['rate'])} {ci_pct(a['observed_verified_rate_cluster_boot95'])}",
                     rnd(a["verified_per_call"]), kn(a["verified_about_true_root"], a["observed_about_true_root"]),
                     str(a["observed_off_target"]),
                     "--" if e["n"] == 0 else f"{kn(e['verified'], e['n'])} ({e['reversed']})"])
    p = D["cc"]["paired"]["claims_E1_vs_B4"]
    return Table("exp_claims", EXPLORATORY,
                 "Claim-level verification by the frozen deterministic verifier over all 265 views (verifier "
                 "acceptance, not human-established truth).",
                 ["Claims from", "Calls /\ndiag.", "Claims", "Verified /\nobserved", "Rate\n[cluster 95% CI]",
                  "Verified\nper call", "Root claims\nverified / obs.", "Off-target\nobserved",
                  "Edges verified\n(reversed)"], rows,
                 ["Rate CI: cluster bootstrap over base cases. Root claims: subject is the ground-truth root. "
                  "Off-target: subject is neither the root nor a known symptom service. E0 rows cover its 233 "
                  "alert-window diagnoses.",
                  f"Paired by base case (n = {p['n_base_cases']}), E1 - B4 verified rate "
                  f"{rnd(p['verified_rate_mean_case_difference'], 3, sign=True)} "
                  f"{ci(p['verified_rate_cluster_boot95'], 3)}; verified root claims per case "
                  f"{rnd(p['verified_root_claims_mean_case_difference'], sign=True)} "
                  f"{ci(p['verified_root_claims_cluster_boot95'])}. E1's model writes no more verifiable claims than "
                  "B4's, but more of them concern the true root (it is shown E0's ranking).",
                  "§ E2 canonical claims are derived by deterministic code from E1's citations; they are never "
                  "counted as model-written claims.",
                  "Verifier precision and recall are unknown until the blinded human audit is adjudicated."],
                 wide=True, align="lrrrrrrrr")


FIRST_FAIL = [("subject_association_error", "Subject /\nownership V3"), ("unit_or_schema_misuse", "Unit /\nschema V0"),
              ("value_miscopied", "Value\nV4"), ("not_above_baseline", "Not above\nbaseline V5"),
              ("out_of_window_citation", "Out of\nwindow V2"), ("unknown_citation", "Unknown\ncitation V1")]


def t_exp_rejections(D):
    C = D["cm"]["conditions"]
    rows = []
    for c, name in (("B4", "B4 (LLM)"), ("E1", "E1 (LLM)"), ("E2-canonical", "E2 canonical§"),
                    ("E0", "E0 templates")):
        a = C[c]["all"]
        bf = a["errors"]["by_first_failed_check"]
        assert sum(bf.values()) == a["observed_rejected"], c
        cit = a["citations"]
        rows.append([name, str(a["observed_rejected"])] + [str(bf.get(k, 0)) for k, _ in FIRST_FAIL]
                    + ["--" if not cit else kn(cit["valid_ids_but_rejected"], a["observed_rejected"])])
    return Table("exp_rejections", EXPLORATORY,
                 "Why observed claims were rejected: first failed verifier check.",
                 ["Claims from", "Rejected", *[n for _, n in FIRST_FAIL], "Citations\nall valid"], rows,
                 ["Each rejected claim is counted once, at its first failed check. LLM claims almost always cite "
                  "records that exist; they fail on which service the evidence belongs to, units, copied values, or "
                  "values not above baseline.", "§ system-derived, not model-written."],
                 wide=True, align="lrrrrrrrr")


def t_exp_e2(D):
    E = D["cm"]["conditions"]["E2_ownership"]
    rows_ = D["cm"]["e2_ownership_rows"]
    obs = [r for r in rows_ if r["kind"] == "observed"]
    n = E["original_observed"]
    disp = Counter(r["disposition"] for r in obs)
    assert len(obs) == n and all(disp[k] == E["dispositions"][k] for k in ("consistent", "transformed", "rejected"))
    na_obs, na_inf = disp["not_applicable"], sum(r["kind"] == "inferred" for r in rows_)
    assert na_obs + na_inf == E["dispositions"]["not_applicable"]
    w = lambda k: f"{pct(k / n)} {ci_pct(wilson(k, n))}"
    rows = [["Consistent (kept as written)", kn(disp["consistent"], n), w(disp["consistent"])],
            ["Transformed into a canonical claim", kn(disp["transformed"], n), w(disp["transformed"])],
            ["   with a subject-ownership correction", str(E["subject_ownership_errors"]), ""],
            ["   with a unit change", str(E["unit_changes"]), ""],
            ["Rejected (no admissible repair)", kn(disp["rejected"], n), w(disp["rejected"])]]
    rows += [[f"   reason: {k}", str(v), ""] for k, v in E["rejected_reasons"].items()]
    rows += [["Not applicable (relational onset_before)", kn(na_obs, n), w(na_obs)], "MIDRULE",
             ["Canonical claims verified", kn(E["canonical_verified"]["k"], E["canonical_verified"]["n"]),
              f"{pct(E['canonical_verified']['rate'])} {ci_pct(E['canonical_verified']['wilson95'])}"],
             ["   first failed check V4 / V5", f"{E['canonical_failed_by_first_check']['V4']} / "
                                               f"{E['canonical_failed_by_first_check']['V5']}", ""],
             ["   verified and about the true root",
              kn(E["canonical_verified_about_true_root"], E["canonical_verified"]["k"]), ""],
             ["Verified per call: original / + canonical",
              f"{rnd(E['verified_per_call']['original_only'])} / "
              f"{rnd(E['verified_per_call']['original_plus_canonical'])}", ""]]
    return Table("exp_e2", EXPLORATORY,
                 f"E2 deterministic evidence ownership applied to E1's {n} observed claims (no model call).",
                 ["Disposition", "Count", "Rate [Wilson 95%]"], rows,
                 [f"E1's {na_inf} inferred claims are outside E2's scope. A transformed claim may carry both a "
                  "subject and a unit change.",
                  "Canonical claims are system-derived and reported separately from model-written claims. E2 "
                  "leaves localization unchanged and adds few verified root claims; it shares association rules "
                  "with the verifier, so its verified rate is not independent evidence."], align="lrr")


def t_exp_controls(D):
    cc = D["cc"]
    own, g = cc["llm_own"], cc["b4g_gate"]
    cs = g["counterfactual_call_saving"]
    Dg = cc["diagnosis"]
    w = lambda x: f"{kn(x['k'], x['n'])} {ci(x['wilson95'])}"
    rows = [["B4 model's own ABSTAIN on controls", w(own["B4_own_ABSTAIN_on_controls"])],
            ["B4 model's own ABSTAIN on fault views", w(own["B4_own_ABSTAIN_on_fault_views"])],
            ["E1 model's own ABSTAIN on the controls it saw", w(own["E1_llm_own_ABSTAIN_on_controls"])],
            ["B4: controls diagnosed", w(Dg["B4"]["all"]["control_false_diagnoses"])],
            ["E1 (window rule): controls diagnosed", w(Dg["E1"]["all"]["control_false_diagnoses"])],
            ["E1 frozen-gated (= E0): controls diagnosed", w(Dg["E1-frozen-gated"]["all"]["control_false_diagnoses"])],
            "MIDRULE",
            ["B4-G: controls diagnosed", w(Dg["B4-G"]["all"]["control_false_diagnoses"])],
            ["B4-G gate: fault views missed", w(g["missed_fault_views"])],
            ["B4-G gate: correct B4 top-1 lost", str(cs["b4_correct_top1_lost_by_gate"])],
            ["B4-G counterfactual: calls avoided",
             f"{cs['calls_avoided']} of {cs['b4_calls_ungated']} ({cs['calls_avoided_on_controls']} control + "
             f"{cs['calls_avoided_on_fault_views']} fault views; {rnd(cs['llm_seconds_avoided'] / 60, 0)} min)"]]
    return Table("exp_controls", EXPLORATORY,
                 "Healthy controls, the LLM's own abstention, and the detector gate (6 control cases × 5 views).",
                 ["Quantity", "k/n [Wilson 95%]"], rows,
                 ["B4's model never abstained on a healthy control. B4-G removes all control false diagnoses only "
                  "by adding the frozen detector; its 2 missed fault views are both strong CPU-pressure faults "
                  "under MT (B4 was wrong on both). On the dev seeds the same gate missed 49/342 fault views.",
                  "The calls B4-G avoids were in fact made (B4 is ungated); the saving is counterfactual. Control "
                  "results rest on only 6 base cases."], align="lr")


def t_exp_runtime(D):
    R = D["rt"]
    rows = []
    for c in ("B4", "E1"):
        p = R["per_condition"][c]
        rows.append([c, str(p["calls"]), f"{rnd(p['wall_s']['mean'], 1)} / {rnd(p['wall_s']['p95'], 1)}",
                     f"{rnd(p['prompt_tokens']['mean'], 0)} ({p['prompt_tokens']['max']})",
                     f"{rnd(p['output_tokens']['mean'], 0)} ({p['output_tokens']['max']})",
                     rnd(p["wall_s"]["total_h"])])
    rt = D["final"]["runtime"]["per_method"]["E0"]
    return Table("exp_runtime", EXPLORATORY,
                 "LLM cost on one 8 GB Apple M1 laptop (qwen3:4b via Ollama, one call at a time).",
                 ["Condition", "Calls", "Latency (s)\nmean / p95", "Prompt tokens\nmean (max)",
                  "Output tokens\nmean (max)", "Total\n(h)"], rows,
                 [f"All {R['n_completed_calls']} calls schema-valid; 0 timeouts, retries or truncations; Ollama "
                  f"peak RSS {rnd(R['ollama_rss_peak_mb'] / 1024, 1)} GB with heavy swap; API cost $0. For "
                  f"comparison, deterministic E0 ranks in {rnd(rt['rank_p50_s'] * 1000, 0)} ms and verifies in "
                  f"{rnd(rt['verify_p50_s'] * 1000, 0)} ms (medians)."], align="lrrrrr")


# ---------------------------------------------------------------- figures
def fig_conf_localization(D):
    F = D["final"]
    views = ["C0", "R40", "MT", "ML", "MM", "CS", "CF"]
    fig = Fig(7.16, 2.3)
    ax = Axes(fig, 40, 22, 7.16 * 72 - 52, 2.3 * 72 - 50, (-0.5, len(views) - 0.5), (0, 1))
    ax.yaxis([0, 0.2, 0.4, 0.6, 0.8, 1.0], "Top-1 accuracy")
    ax.xaxis(list(range(len(views))), [two_line(v) for v in views])
    off = {m: (i - 2) * 0.14 for i, m in enumerate(METHODS)}
    for i, v in enumerate(views):
        for m in METHODS:
            r = acc_row(F, m, v)
            lo, hi = r["top1_correct_rate_all_faults_ci"]
            ax.errorbar(i + off[m], r["top1_correct_rate_all_faults"], max(lo, 0), hi, COLOR[m], MARKER[m],
                        filled=(m == "E0"))
    legend(fig, 42, 13, [(m if m != "E0" else "E0 (proposed)", COLOR[m], MARKER[m]) for m in METHODS])
    fig.text(7.16 * 72 - 8, 13, "47 fault cases; 95% CI (Wilson; R40: cluster bootstrap)", size=6.5, anchor="end",
             italic=True)
    return fig


def fig_conf_fault_heatmap(D):
    F = D["final"]["per_fault_type_C0"]
    fig = Fig(3.5, 2.4)
    x0, y0, cw, ch = 88, 26, 31, 22
    for j, m in enumerate(METHODS):
        fig.text(x0 + j * cw + cw / 2, y0 - 6, m, size=6.8, anchor="middle", weight="bold" if m == "E0" else "normal")
    for i, ft in enumerate(FAULTS):
        n = F[ft]["E0"]["n"]
        fig.text(x0 - 4, y0 + i * ch + ch / 2 + 2.5, f"{FAULT_NAME[ft]} (n={n})", size=6.6, anchor="end")
        for j, m in enumerate(METHODS):
            k = F[ft][m]["top1"]
            r = k / n
            shade = int(255 - r * (255 - 60))
            fill = f"#{shade:02X}{int(255 - r * (255 - 114)):02X}{int(255 - r * (255 - 178)):02X}"
            fig.rect(x0 + j * cw, y0 + i * ch, cw, ch, fill, "#FFFFFF", 1)
            fig.text(x0 + j * cw + cw / 2, y0 + i * ch + ch / 2 + 2.5, f"{k}/{n}", size=6.8, anchor="middle",
                     color="#FFFFFF" if r > 0.6 else "#000000")
    fig.text(x0, y0 + len(FAULTS) * ch + 11, "Clean view C0; cell = top-1 correct / cases", size=6.3, italic=True)
    return fig


def fig_conf_risk_coverage(D):
    tr = D["trials"]
    A = D["final"]["abstention"]
    fig = Fig(3.5, 2.5)
    ax = Axes(fig, 36, 14, 3.5 * 72 - 46, 2.5 * 72 - 46, (0, 1), (0, 0.8))
    ax.yaxis([0, 0.2, 0.4, 0.6, 0.8], "Selective risk (top-1 error)")
    ax.xaxis([0, 0.2, 0.4, 0.6, 0.8, 1.0], label="Coverage (share of diagnosed fault trials kept)")
    series = [("E0:A-margin", COLOR["E0"], None), ("E0:S", COLOR["E0-A-graph"], "3,2"), ("B2:margin", COLOR["B2"], "1,1.5")]
    ents = []
    for name, col, dash in series:
        m, get = SCORES[name]
        items = [(get(t), 0 if t["top1_correct"] else 1) for t in tr if t["method"] == m and t["anchor"] == "alert"
                 and t["fault_type"] != "none" and t["decision"] == "DIAGNOSE"]
        aurc, curve = rc(items)
        assert round(aurc, 4) == A[name]["aurc"] and len(items) == A[name]["n_diagnosed_fault_trials"], name
        n = len(curve)
        pts = [(ax.X((k + 1) / n), ax.Y(min(e, 0.8))) for k, e in enumerate(curve) if k >= 4]  # skip k<5 (noisy)
        fig.polyline(pts, col, 1.1, dash)
        for op in ("op1", "op2"):
            o = A[name][op]
            if o.get("tau") is not None and o.get("selective_risk") is not None and o["coverage"] > 0.01:
                fig.marker(ax.X(o["coverage"]), ax.Y(o["selective_risk"]), "circle" if op == "op1" else "square",
                           col, size=2.2, filled=op == "op1")
        ents.append((f"{SCORE_NAME[name]} (AURC {rnd(A[name]['aurc'], 3)}, n={n})", col, "line"))
    legend(fig, ax.x0 + 6, ax.y0 + 10, ents, size=6.2, horizontal=False, step=9)
    fig.text(ax.x0 + 6, ax.y0 + 38, "● op1   ■ op2 (thresholds fixed on calibration)", size=6.0, italic=True)
    return fig


def fig_exp_diagnosis(D):
    Dg = D["cc"]["diagnosis"]
    fig = Fig(7.16, 2.3)
    W, H = 7.16 * 72, 2.3 * 72
    ax = Axes(fig, 40, 22, W * 0.52, H - 52, (-0.5, 4.5), (0, 1))
    ax.yaxis([0, 0.2, 0.4, 0.6, 0.8, 1.0], "Top-1 accuracy (47 cases)")
    ax.xaxis(list(range(5)), [two_line(v) for v in VIEWS_P4B], size=6.8)
    for i, v in enumerate(VIEWS_P4B):
        for c, dx in (("B4", -0.12), ("E1", 0.12)):
            t = Dg[c]["by_spec"][v]["top1"]
            ax.errorbar(i + dx, t["rate"], t["wilson95"][0], t["wilson95"][1], COLOR[c], MARKER[c], filled=c == "E1")
    legend(fig, 42, 13, [("B4: LLM only", COLOR["B4"], "square"),
                         ("E1: E0 localization + LLM claims (= frozen E0)", COLOR["E1"], "circle")], size=6.6)
    # right panel: controls falsely diagnosed
    x0 = W * 0.52 + 140
    bx = Axes(fig, x0, 22, W - x0 - 22, H - 52, (0, 1), (-0.5, 3.5))
    labels = [("B4", "B4"), ("E1", "E1 (window rule)"), ("E1-frozen-gated", "E1 frozen gated"),
              ("B4-G", "B4-G (hybrid)")]
    for i, (c, lab) in enumerate(labels):
        x = Dg[c]["all"]["control_false_diagnoses"]
        y = 3 - i
        bx.errorbar(x["rate"], y, max(x["wilson95"][0], 0), x["wilson95"][1], COLOR[c], MARKER.get(c, "circle"),
                    horizontal=True)
        fig.text(bx.x0 - 6, bx.Y(y) + 2.4, f"{lab}  {x['k']}/{x['n']}", size=6.8, anchor="end")
    bx.xaxis([0, 0.5, 1.0], label="Controls falsely diagnosed (of 30)", fmt=lambda v: f"{v:.1f}", grid=True,
             size=6.8)
    fig.line(bx.x0, bx.y0, bx.x0, bx.y0 + bx.h)
    g = D["cc"]["b4g_gate"]["missed_fault_views"]
    fig.text(bx.x0 + bx.w, 13, f"B4-G gate misses {g['k']}/{g['n']} fault views", size=6.4, anchor="end",
             italic=True)
    return fig


def fig_exp_claims(D):
    C = D["cm"]["conditions"]
    segs = [("verified", "Verified", "#0072B2"), ("subject_association_error", "Subject/ownership V3", "#D55E00"),
            ("unit_or_schema_misuse", "Unit/schema V0", "#E69F00"), ("value_miscopied", "Value V4", "#CC79A7"),
            ("not_above_baseline", "Not above baseline V5", "#F0E442"), ("other", "Other (V1/V2)", "#999999")]
    rows = [("B4", "B4 (LLM)"), ("E1", "E1 (LLM)"), ("E2-canonical", "E2 canonical (system)"), ("E0", "E0 templates")]
    fig = Fig(7.16, 1.85)
    W = 7.16 * 72
    ax = Axes(fig, 118, 22, W - 130, 1.85 * 72 - 52, (0, 1), (-0.5, len(rows) - 0.5))
    bh = 13
    for i, (c, lab) in enumerate(rows):
        a = C[c]["all"]
        bf = a["errors"]["by_first_failed_check"]
        vals = {"verified": a["observed_verified"], "other": bf.get("out_of_window_citation", 0)
                + bf.get("unknown_citation", 0), **{k: bf.get(k, 0) for k, _, _ in segs[1:5]}}
        n = a["observed"]
        assert sum(vals.values()) == n, c
        y = ax.Y(len(rows) - 1 - i)
        x = 0.0
        fig.text(ax.x0 - 5, y + 2.4, f"{lab}  n={n:,}", size=6.8, anchor="end")
        for k, _, col in segs:
            w = vals[k] / n
            if w > 0:
                fig.rect(ax.X(x), y - bh / 2, ax.X(x + w) - ax.X(x), bh, col, "#FFFFFF", 0.4)
                if w >= 0.06:
                    fig.text(ax.X(x + w / 2), y + 2.4, f"{100 * w:.0f}%", size=6.3, anchor="middle",
                             color="#FFFFFF" if col in ("#0072B2", "#D55E00") else "#000000")
            x += w
    ax.xaxis([0, 0.25, 0.5, 0.75, 1.0], fmt=lambda v: f"{int(v * 100)}%", size=6.6,
             label="Share of observed claims (frozen verifier; first failed check)")
    legend(fig, ax.x0, 12, [(n, col, "patch") for _, n, col in segs], size=6.4, gap=6)
    return fig


# ---------------------------------------------------------------- number macros
def numbers(D) -> dict:
    F, cc, cm = D["final"], D["cc"], D["cm"]
    e0c0 = acc_row(F, "E0", "C0")
    a = F["abstention"]
    d = cc["diagnosis"]
    N = {
        "TestFaultCases": 47, "TestControlCases": 6,
        "EzeroCzeroTopOneK": e0c0["top1_correct_of_triggered"], "EzeroCzeroTopOne": rnd(e0c0["top1_correct_rate_all_faults"]),
        "EzeroCzeroTopOneCI": ci(e0c0["top1_correct_rate_all_faults_ci"]),
        "BtwoCzeroTopOne": rnd(acc_row(F, "B2", "C0")["top1_correct_rate_all_faults"]),
        "EzeroVsBtwoDiff": rnd(F["paired_comparisons"]["C0"]["E0_vs_B2"]["mean_diff_top1"], sign=True),
        "EzeroVsBtwoCI": ci(F["paired_comparisons"]["C0"]["E0_vs_B2"]["ci_cluster_bootstrap"]),
        "EzeroMTTopOne": rnd(acc_row(F, "E0", "MT")["top1_correct_rate_all_faults"]),
        "EzeroRfortyTopOne": rnd(acc_row(F, "E0", "R40")["top1_correct_rate_all_faults"]),
        "AURCS": rnd(a["E0:S"]["aurc"], 3), "AURCMargin": rnd(a["E0:A-margin"]["aurc"], 3),
        "MarginOpTwoCoverage": rnd(a["E0:A-margin"]["op2"]["coverage"]),
        "MarginOpTwoRisk": rnd(a["E0:A-margin"]["op2"]["selective_risk"], 3),
        "EzeroVerifiedRateCzero": pct(F["verification"]["by_condition"]["C0"]["verified_rate"]),
        "LLMCalls": cc["n_completed_calls"],
        "BfourTopOneK": d["B4"]["all"]["top1"]["k"], "BfourTopOne": rnd(d["B4"]["all"]["top1"]["rate"], 3),
        "EoneTopOneK": d["E1"]["all"]["top1"]["k"], "EoneTopOne": rnd(d["E1"]["all"]["top1"]["rate"], 3),
        "FaultViews": d["B4"]["all"]["top1"]["n"], "ControlViews": d["B4"]["all"]["control_false_diagnoses"]["n"],
        "BfourControlFalseK": d["B4"]["all"]["control_false_diagnoses"]["k"],
        "EoneControlFalseK": d["E1"]["all"]["control_false_diagnoses"]["k"],
        "BfourGMissedK": cc["b4g_gate"]["missed_fault_views"]["k"],
        "BfourGCallsAvoided": cc["b4g_gate"]["counterfactual_call_saving"]["calls_avoided"],
        "BfourVerifiedK": cm["conditions"]["B4"]["all"]["observed_verified"],
        "BfourObservedN": cm["conditions"]["B4"]["all"]["observed"],
        "BfourVerifiedRate": pct(cm["conditions"]["B4"]["all"]["observed_verified_rate"]["rate"]),
        "EoneVerifiedK": cm["conditions"]["E1"]["all"]["observed_verified"],
        "EoneObservedN": cm["conditions"]["E1"]["all"]["observed"],
        "EoneVerifiedRate": pct(cm["conditions"]["E1"]["all"]["observed_verified_rate"]["rate"]),
        "ClaimsPairedDiff": rnd(cc["paired"]["claims_E1_vs_B4"]["verified_rate_mean_case_difference"], 3, sign=True),
        "ClaimsPairedCI": ci(cc["paired"]["claims_E1_vs_B4"]["verified_rate_cluster_boot95"], 3),
        "LLMHours": rnd(D["rt"]["elapsed_first_to_last_call_h"], 1),
        "LLMMeanLatency": rnd(D["rt"]["per_condition"]["B4"]["wall_s"]["mean"], 0),
        "AuditItems": 240,
    }
    return N


def numbers_tex(N: dict) -> str:
    out = ["% Auto-generated by paper/build_artifacts.py from recorded results; do not edit by hand.",
           "% Use these macros in the manuscript instead of typing numbers."]
    for k, v in N.items():
        assert k.isalpha(), k
        out.append(f"\\newcommand{{\\num{k}}}{{{tex(str(v))}}}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- main
FIG_SPECS = [("fig_conf_localization", fig_conf_localization, CONFIRMATORY,
              "Top-1 localization by telemetry view for E0 and the baselines (47 held-out fault cases)."),
             ("fig_conf_fault_types", fig_conf_fault_heatmap, CONFIRMATORY,
              "Top-1 localization by fault type on C0; cascading failure and strong CPU pressure remain unsolved."),
             ("fig_conf_risk_coverage", fig_conf_risk_coverage, CONFIRMATORY,
              "Risk-coverage curves of E0's abstention scores and B2's margin on the test seeds; the pre-registered "
              "composite S is worse than the margin alone."),
             ("fig_exp_diagnosis", fig_exp_diagnosis, EXPLORATORY,
              "B4 (LLM only) versus E1 (E0 localization with LLM-written claims) by view, and false diagnoses on "
              "healthy controls."),
             ("fig_exp_claims", fig_exp_claims, EXPLORATORY,
              "Frozen-verifier outcome of observed claims (acceptance, not human-verified truth).")]
TABLE_SPECS = [t_conf_localization, t_conf_fault_types, t_conf_paired, t_conf_abstention, t_conf_verifier,
               t_exp_diagnosis, t_exp_views, t_exp_fault_types, t_exp_claims, t_exp_rejections, t_exp_e2,
               t_exp_controls, t_exp_runtime]


def build(convert=True) -> dict:
    D = load()
    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    outputs = {}
    md = ["# Paper tables (auto-generated preview)", "",
          "Generated by `paper/build_artifacts.py` from recorded results only. The `.tex` files are the "
          "camera-ready versions (they need `\\usepackage{booktabs}`).", ""]
    for fn in TABLE_SPECS:
        t = fn(D)
        p = TABLES / f"{t.tid}.tex"
        p.write_text(t.latex())
        outputs[str(p.relative_to(REPO))] = sha(p)
        md.append(t.markdown())
    p = TABLES / "README.md"
    p.write_text("\n".join(md))
    outputs[str(p.relative_to(REPO))] = sha(p)
    figmd = ["# Paper figures (auto-generated)", "",
             "SVG is the source; PDF (for LaTeX) and PNG (preview) are converted with `rsvg-convert`.", ""]
    for name, fn, tier, cap in FIG_SPECS:
        svg = FIGURES / f"{name}.svg"
        svg.write_text(fn(D).svg())
        outputs[str(svg.relative_to(REPO))] = sha(svg)
        figmd += [f"## `{name}`", "", f"*{tier}.* {cap}", "", f"![{name}]({name}.png)", ""]
        if convert and shutil.which("rsvg-convert"):
            env = {**os.environ, "SOURCE_DATE_EPOCH": "1792022400"}
            subprocess.run(["rsvg-convert", "-f", "pdf", "-o", str(FIGURES / f"{name}.pdf"), str(svg)], check=True,
                           env=env)
            subprocess.run(["rsvg-convert", "-f", "png", "-d", "200", "-p", "200", "-o",
                            str(FIGURES / f"{name}.png"), str(svg)], check=True, env=env)
    p = FIGURES / "README.md"
    p.write_text("\n".join(figmd))
    outputs[str(p.relative_to(REPO))] = sha(p)
    p = PAPER / "numbers.tex"
    p.write_text(numbers_tex(numbers(D)))
    outputs[str(p.relative_to(REPO))] = sha(p)
    man = {"generator": "paper/build_artifacts.py", "generator_sha256": sha(PAPER / "build_artifacts.py"),
           "svgfig_sha256": sha(PAPER / "svgfig.py"),
           "inputs": {v: sha(REPO / v) for v in INPUTS.values()},
           "outputs": outputs,
           "not_hashed": "PDF/PNG conversions (renderer-dependent bytes); rebuild them from the hashed SVGs",
           "tiers": {"confirmatory": CONFIRMATORY, "exploratory": EXPLORATORY}}
    (PAPER / "artifact_manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    return man


if __name__ == "__main__":
    m = build()
    print(f"{len(m['outputs'])} hashed outputs; inputs verified by recomputation asserts")
