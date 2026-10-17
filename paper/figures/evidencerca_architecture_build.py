"""Writes evidencerca_architecture_source.dot (the system schematic) and renders it with the local Graphviz.

  uv run python paper/figures/evidencerca_architecture_build.py

Every node is placed by hand (points, origin top-left, converted to Graphviz's bottom-left) and drawn with
`neato -n2`, so arrows are straight, axis-aligned and cannot cross. Edges run between invisible anchor points.
Nothing here reads results or code; the component-to-source mapping is in docs/architecture_diagram_validation.md.
"""
from __future__ import annotations

import subprocess
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "evidencerca_architecture"
W, H = 516, 246                      # points: 7.17 in x 3.42 in (aspect 2.10), full IEEE two-column width
FONT = "Helvetica"
BODY, TITLE, GROUP = 6.2, 6.6, 6.6   # font sizes in points at the printed size

INK, MUTED = "#1A1A1A", "#555555"
BLUE, BLUE_FILL, BLUE_FRAME = "#2B5C8A", "#E9F0F7", "#9DB3C8"
GREY, GREY_FILL = "#5A5A5A", "#F0F0F0"
STYLE = {   # fill, border, border width, dash
    "det": (BLUE_FILL, BLUE, 0.6, False),       # deterministic component
    "out": ("#FFFFFF", BLUE, 1.3, False),       # output
    "llm": (GREY_FILL, GREY, 0.6, False),       # LLM call
    "opt": ("#FFFFFF", GREY, 0.6, True),        # optional / exploratory branch
    "gt": ("#FFFFFF", MUTED, 0.6, True),        # evaluation-only, outside the inference pipeline
    "svc": ("#FFFFFF", BLUE, 0.5, False),       # testbed service
}

lines: list[str] = []
_n = [0]


def _id(prefix="n") -> str:
    _n[0] += 1
    return f"{prefix}{_n[0]}"


def _pos(x, y) -> str:
    return f'"{x:.2f},{H - y:.2f}!"'


def _html(rows, size=BODY, align="CENTER") -> str:
    """rows: str (plain), ('b', str) bold, ('i', str) italic, ('L', str) left-aligned plain."""
    out = []
    for r in rows:
        kind, s = (r if isinstance(r, tuple) else ("", r))
        t = escape(s)
        if kind == "b":
            t = f"<B>{t}</B>"
        elif kind == "i":
            t = f"<I>{t}</I>"
        brk = '<BR ALIGN="LEFT"/>' if (kind == "L" or align == "LEFT") else "<BR/>"
        out.append(t + brk)
    return f'<<FONT POINT-SIZE="{size}" COLOR="{INK}">{"".join(out)}</FONT>>'


def box(x, y, w, h, rows, style="det", size=BODY, align="CENTER") -> tuple:
    fill, border, pw, dash = STYLE[style]
    st = "filled,dashed" if dash else "filled"
    nid = _id("b")
    lines.append(f'{nid} [pos={_pos(x + w / 2, y + h / 2)}, width={w / 72:.4f}, height={h / 72:.4f}, '
                 f'shape=box, style="{st}", fillcolor="{fill}", color="{border}", penwidth={pw}, '
                 f'label={_html(rows, size, align)}];')
    return (x, y, w, h)


def frame(x, y, w, h, title, color=BLUE_FRAME, dash=False):
    nid = _id("f")
    st = "dashed" if dash else "solid"
    lines.append(f'{nid} [pos={_pos(x + w / 2, y + h / 2)}, width={w / 72:.4f}, height={h / 72:.4f}, '
                 f'shape=box, style="{st}", color="{color}", penwidth=0.6, label=""];')
    text(x + 4, y + 7.6, title, size=GROUP, bold=True, color=BLUE, width=w - 8)


def text(x, y, s, size=BODY, bold=False, color=INK, anchor="left", italic=False, width=None):
    """Single line of text, vertically centred on y. anchor="left": starts exactly at x, inside a fixed-width,
    left-aligned cell of `width` points (default: up to the canvas edge); anchor="centre": centred on x."""
    nid = _id("t")
    t = escape(s)
    if bold:
        t = f"<B>{t}</B>"
    if italic:
        t = f"<I>{t}</I>"
    w = (width or (W - x - 2)) if anchor == "left" else width
    cx = x + w / 2 if anchor == "left" else x
    align = "LEFT" if anchor == "left" else "CENTER"
    cell = (f'<TABLE BORDER="0" CELLPADDING="0" CELLSPACING="0"><TR><TD FIXEDSIZE="TRUE" WIDTH="{w:.0f}" '
            f'HEIGHT="{size * 1.2:.0f}" ALIGN="{align}"><FONT POINT-SIZE="{size}" COLOR="{color}">{t}</FONT></TD></TR></TABLE>')
    lines.append(f'{nid} [pos={_pos(cx, y)}, shape=plaintext, width={(w + 1) / 72:.4f}, '
                 f'height={(size * 1.2 + 2) / 72:.4f}, fixedsize=true, label=<{cell}>];')


def _pt(x, y) -> str:
    nid = _id("p")
    lines.append(f'{nid} [pos={_pos(x, y)}, shape=point, width=0.01, height=0.01, style=invis, label=""];')
    return nid


def arrow(*pts, dashed=False, color=INK, head=True, width=0.75):
    """Polyline through pts (each segment axis-aligned by construction); arrowhead on the last segment."""
    ids = [_pt(x, y) for x, y in pts]
    st = "dashed" if dashed else "solid"
    for i, (a, b) in enumerate(zip(ids, ids[1:])):
        last = i == len(ids) - 2
        ah = "normal" if (head and last) else "none"
        lines.append(f'{a} -> {b} [arrowhead={ah}, arrowsize=0.42, penwidth={width}, color="{color}", style={st}];')


def link(a: str, b: str, **kw):
    """Straight edge between two named nodes, clipped at their borders (testbed topology only)."""
    lines.append(f'{a} -> {b} [arrowhead=normal, arrowsize=0.38, penwidth=0.55, color="{BLUE}"];')


def service(name, cx, cy, w=40, h=12):
    nid = _id("s")
    fill, border, pw, _ = STYLE["svc"]
    lines.append(f'{nid} [pos={_pos(cx, cy)}, width={w / 72:.4f}, height={h / 72:.4f}, shape=box, '
                 f'style="filled", fillcolor="{fill}", color="{border}", penwidth={pw}, '
                 f'label=<<FONT POINT-SIZE="{BODY}" COLOR="{INK}">{escape(name)}</FONT>>];')
    return nid


def build() -> str:
    # ---------------------------------------------------------------- column 1: testbed, telemetry, labels
    frame(4, 4, 92, 88, "A. Microservice testbed")
    gw = service("API gateway", 50, 26, w=46)
    od = service("order", 27, 49)
    inv = service("inventory", 73, 49)
    pay = service("payment", 27, 72)
    pg = service("PostgreSQL", 73, 72)
    for a, b in ((gw, od), (gw, inv), (od, inv), (od, pay), (od, pg), (inv, pg)):
        link(a, b)
    text(50, 85, "HTTP/SQL calls · faults injected here", size=5.8, color=MUTED, anchor="centre", width=90)

    frame(4, 98, 92, 92, "B. Observability")
    box(9, 111, 82, 33, [("b", "OpenTelemetry"), "traces · metrics · logs", "logs carry trace/span IDs",
                         "→ checksummed raw bundle"])
    box(9, 151, 82, 34, [("b", "Normalization"), "typed evidence records", "ID = content hash (ev-…)",
                         "emitter · kind · callee · t"])
    arrow((84, 92), (84, 111))
    arrow((50, 144), (50, 151))

    box(4, 196, 92, 27, [("b", "Fault-injection labels"), "sealed from the pipeline;", "scoring + view building only"],
        style="gt", size=5.9)

    # legend: two rows of two entries
    def swatch(x, y, st):
        fill, border, pw, _ = STYLE[st]
        nid = _id("k")
        lines.append(f'{nid} [pos={_pos(x + 4, y)}, width={8 / 72:.4f}, height={6 / 72:.4f}, shape=box, '
                     f'style=filled, fillcolor="{fill}", color="{border}", penwidth={pw}, label=""];')
    for (x, y, st, lab) in ((5, 230, "det", "deterministic"), (52, 230, "llm", "LLM call"),
                            (5, 240, "out", "output")):
        swatch(x, y, st)
        text(x + 11, y, lab, size=5.8, width=38)
    for x0 in (51, 55.5, 60):                       # drawn as three dashes: a 12 pt dashed line shows only one
        arrow((x0, 240), (x0 + 3, 240), head=False, width=0.7)
    text(66, 240, "optional", size=5.8, width=30)

    # ---------------------------------------------------------------- column 2: evidence view + shared detector
    box(102, 4, 44, 238, [("b", "Evidence"), ("b", "view"), "", "one case,", "one view:", "clean (C0)", "or 1 of 11",
                          "perturbations", "", "typed records", "with ev-IDs", "", "", ("b", "Shared"), ("b", "alert"),
                          ("b", "detector"), "", "gateway SLO", "→ analysis +", "baseline", "windows"])
    arrow((91, 168), (102, 168))

    # ---------------------------------------------------------------- column 3a: E0 (deterministic)
    frame(152, 4, 250, 92, "C–D. E0: localization + evidence retrieval (deterministic, no LLM)")
    box(160, 17, 72, 32, [("b", "Anomaly signals"), "per service", "robust baseline (MAD)", "onset times"])
    box(160, 56, 72, 34, [("b", "Observed call graph"), "caller → callee spans", "invocation edges,",
                          ("i", "not a causal graph")])
    box(242, 17, 74, 73, [("b", "Constrained scoring"), "a caller whose callee", "is anomalous no later",
                          "than itself (+10 s):", "its latency, error and", "log signals count as",
                          "symptoms (inferred", "propagation)"])
    box(326, 17, 72, 32, [("b", "Ranked root"), ("b", "candidates"), "+ fault-type rule"], style="out")
    box(326, 56, 72, 34, [("b", "Evidence retrieval"), "cited ev-IDs: root", "+ propagation path",
                          "→ template claims"])
    arrow((146, 33), (160, 33))
    arrow((146, 73), (160, 73))
    arrow((232, 33), (242, 33))
    arrow((232, 73), (242, 73))
    arrow((316, 33), (326, 33))
    arrow((362, 49), (362, 56))

    # ---------------------------------------------------------------- column 3b: E1 (E0-guided LLM explanation)
    frame(152, 110, 250, 62, "E1: E0-guided LLM explanation (exploratory)")
    box(160, 123, 76, 44, [("b", "Evidence pack"), "E0's records first,", "neutral fill to ≤36",
                           "+ digest, call graph"])
    box(246, 123, 90, 44, [("b", "LLM explanation"), "qwen3:4b, local", "writes claims + text;",
                           "ranking stays E0's", "(LLM's pick logged)"], style="llm")
    box(344, 141, 54, 26, [("b", "E2 (optional)"), "provenance →", "canonical claims"], style="opt", size=5.8)
    # E0 hand-off: ranking + retrieved records, around the frame gap
    arrow((362, 90), (362, 103), (300, 103), (300, 123), color=BLUE, width=0.9)
    text(158, 103, "E0 ranking + retrieved ev-IDs (to E1 only)", size=5.9, color=BLUE, italic=True)
    arrow((146, 145), (160, 145))
    arrow((236, 145), (246, 145))
    arrow((336, 154), (344, 154), dashed=True)

    # ---------------------------------------------------------------- column 3c: B4 (LLM-only)
    frame(152, 178, 250, 64, "B4: LLM-only diagnosis (exploratory baseline, no E0 input)")
    box(160, 191, 76, 24, [("b", "Evidence pack"), "method-neutral, ≤36"])
    box(246, 191, 152, 24, [("b", "LLM-only diagnosis"), "same model + budget; own ranking, fault type, claims"],
        style="llm")
    box(160, 221, 238, 17, ["B4-G (optional): shared detector gates B4; no alert ⇒ ABSTAIN, no LLM call"],
        style="opt", size=5.8)
    arrow((146, 203), (160, 203))
    arrow((236, 203), (246, 203))
    arrow((146, 229.5), (160, 229.5), dashed=True)
    arrow((322, 221), (322, 215), dashed=True)

    # ---------------------------------------------------------------- column 4: abstention, verifier, claims
    box(414, 4, 98, 46, [("b", "Abstention (optional)"), "E0 only: S = mean of", "sufficiency, agreement,",
                         "margin, verified coverage", "ABSTAIN if S < τ"], style="out")
    box(414, 56, 98, 150, [("b", "F. Evidence verifier"), "deterministic, independent", "code; re-reads the view",
                           "", ("L", "V0  schema, vocabulary, unit"), ("L", "V1  cited ev-ID exists"),
                           ("L", "V2  case/view ID, time window"), ("L", "V3  emitter/callee ownership"),
                           ("L", "V4  value + unit (±5 %)"), ("L", "V5  baseline deviation, onset"),
                           ("L", "V6  edge in observed calls"), "", "checks claims only;",
                           "never changes a ranking"])
    box(414, 212, 98, 30, [("b", "Accepted / rejected claims"), "all kept, none rewritten;",
                           ("i", "accepted ≠ audited truth")], style="out")
    arrow((398, 33), (414, 33))                          # E0 ranking -> abstention (margin)
    arrow((398, 73), (414, 73))                          # E0 template claims
    arrow((336, 132), (414, 132))                        # E1 claims
    arrow((398, 154), (414, 154), dashed=True)           # E2 canonical claims
    arrow((398, 203), (414, 203))                        # B4 / B4-G claims
    arrow((463, 56), (463, 50))                          # verified-claim coverage -> abstention
    arrow((463, 206), (463, 212))

    head = (f'digraph EvidenceRCA {{\n  graph [bb="0,0,{W},{H}", pad=0, margin=0, splines=line, outputorder=nodesfirst, '
            f'fontname="{FONT}"];\n  node [fontname="{FONT}", margin=0, fixedsize=true];\n'
            f'  edge [fontname="{FONT}"];\n')
    corners = (f'  c0 [pos="0,0!", shape=point, width=0.01, style=invis, label=""];\n'
               f'  c1 [pos="{W},{H}!", shape=point, width=0.01, style=invis, label=""];\n')
    return (f"// Generated by evidencerca_architecture_build.py; render with neato -n2 (positions are pinned).\n"
            + head + corners + "".join(f"  {l}\n" for l in lines) + "}\n")


def main():
    src = HERE / f"{NAME}_source.dot"
    src.write_text(build())
    for fmt in ("svg", "pdf"):
        subprocess.run(["neato", "-n2", f"-T{fmt}:cairo" if fmt == "pdf" else f"-T{fmt}", str(src),
                        "-o", str(HERE / f"{NAME}.{fmt}")], check=True)
    subprocess.run(["neato", "-n2", "-Tpng:cairo", "-Gdpi=300", str(src), "-o", str(HERE / f"{NAME}.png")],
                   check=True)


if __name__ == "__main__":
    main()
