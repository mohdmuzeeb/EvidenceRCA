"""Writes evidencerca_architecture_source.dot (the system schematic) and renders it with the local Graphviz.

  uv run python paper/figures/evidencerca_architecture_build.py

Every node is placed by hand (points, origin top-left, converted to Graphviz's bottom-left) and drawn with
`neato -n2`, so arrows are straight, axis-aligned and cannot cross. Edges run between invisible anchor points.
Nothing here reads results or code; the component-to-source mapping is in docs/architecture_diagram_validation.md.
"""
from __future__ import annotations

import os
import subprocess
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "evidencerca_architecture"
W, H = 516, 230                      # points: 7.17 in x 3.19 in (aspect 2.24), full IEEE two-column width
FONT = "Helvetica"
BODY, TITLE, GROUP = 6.6, 7.0, 7.0   # font sizes in points at the printed size

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
            f'HEIGHT="{size * 1.2 + 1:.0f}" ALIGN="{align}"><FONT POINT-SIZE="{size}" COLOR="{color}">{t}</FONT></TD></TR></TABLE>')
    lines.append(f'{nid} [pos={_pos(cx, y)}, shape=plaintext, width={(w + 1) / 72:.4f}, '
                 f'height={(size * 1.2 + 3) / 72:.4f}, fixedsize=true, label=<{cell}>];')


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
    gw = service("API gateway", 50, 29, w=48, h=13)
    od = service("order", 27, 54, h=13)
    inv = service("inventory", 73, 54, h=13)
    pay = service("payment", 27, 79, h=13)
    pg = service("PostgreSQL", 73, 79, h=13)
    for a, b in ((gw, od), (gw, inv), (od, inv), (od, pay), (od, pg), (inv, pg)):
        link(a, b)

    frame(4, 98, 92, 74, "B. Observability")
    box(9, 111, 82, 22, [("b", "OpenTelemetry"), "traces · metrics · logs"])
    box(9, 141, 82, 26, [("b", "Normalization"), "typed records + ev-IDs"])
    arrow((84, 92), (84, 111))
    arrow((50, 133), (50, 141))

    box(4, 180, 92, 24, [("b", "Fault labels"), "evaluation only"], style="gt")

    # legend: two rows of two entries
    def swatch(x, y, st):
        fill, border, pw, _ = STYLE[st]
        nid = _id("k")
        lines.append(f'{nid} [pos={_pos(x + 4, y)}, width={8 / 72:.4f}, height={6 / 72:.4f}, shape=box, '
                     f'style=filled, fillcolor="{fill}", color="{border}", penwidth={pw}, label=""];')
    for (x, y, st, lab) in ((5, 212, "det", "deterministic"), (52, 212, "llm", "LLM call"),
                            (5, 222, "out", "output")):
        swatch(x, y, st)
        text(x + 11, y, lab, size=5.8, width=38)
    for x0 in (51, 55.5, 60):                       # drawn as three dashes: a 12 pt dashed line shows only one
        arrow((x0, 222), (x0 + 3, 222), head=False, width=0.7)
    text(66, 222, "optional", size=5.8, width=30)

    # ---------------------------------------------------------------- column 2: evidence view + shared detector
    box(102, 4, 44, 222, [("b", "Evidence"), ("b", "view"), "", "typed", "records", "", "", "", "",
                          ("b", "Shared"), ("b", "alert"), ("b", "detector"), "", "→ analysis", "window"])
    arrow((91, 154), (102, 154))

    # ---------------------------------------------------------------- column 3a: E0 (deterministic)
    frame(152, 4, 250, 92, "E0: deterministic localization (no LLM)")
    box(160, 18, 70, 32, [("b", "Anomaly"), ("b", "signals"), "per service + onset"])
    box(160, 58, 70, 32, [("b", "Observed"), ("b", "call graph"), ("i", "not causal")])
    box(240, 18, 76, 72, [("b", "Constrained"), ("b", "scoring"), "", "callee anomalous", "first ⇒ caller",
                          "is a symptom"])
    box(326, 18, 72, 32, [("b", "Ranked root"), ("b", "candidates")], style="out")
    box(326, 58, 72, 32, [("b", "Evidence"), ("b", "retrieval"), "cited ev-IDs"])
    arrow((146, 34), (160, 34))
    arrow((146, 74), (160, 74))
    arrow((230, 34), (240, 34))
    arrow((230, 74), (240, 74))
    arrow((316, 34), (326, 34))
    arrow((362, 50), (362, 58))

    # ---------------------------------------------------------------- column 3b: E1 (E0-guided LLM explanation)
    frame(152, 108, 250, 52, "E1: E0-guided LLM explanation (exploratory)")
    box(160, 121, 176, 34, [("b", "LLM explanation (qwen3:4b)"), "writes claims; ranking stays E0's"],
        style="llm")
    box(344, 135, 54, 20, [("b", "E2 (optional)"), "canonical claims"], style="opt", size=5.8)
    arrow((362, 90), (362, 101), (329, 101), (329, 121), color=BLUE, width=0.9)
    text(158, 101, "E0 ranking + evidence (E1 only)", size=6.0, color=BLUE, italic=True, width=150)
    arrow((146, 138), (160, 138))
    arrow((336, 145), (344, 145), dashed=True)

    # ---------------------------------------------------------------- column 3c: B4 (LLM-only)
    frame(152, 166, 250, 60, "B4: LLM-only diagnosis (exploratory, no E0 input)")
    box(160, 179, 238, 21, [("b", "LLM-only diagnosis (same model)"), "own ranking, fault type, claims"],
        style="llm")
    box(160, 206, 238, 15, ["B4-G (optional): no alert ⇒ ABSTAIN, no LLM call"], style="opt", size=5.9)
    arrow((146, 189.5), (160, 189.5))
    arrow((146, 213.5), (160, 213.5), dashed=True)
    arrow((322, 206), (322, 200), dashed=True)

    # ---------------------------------------------------------------- column 4: abstention, verifier, claims
    box(414, 4, 98, 36, [("b", "Abstention (optional)"), "E0 only: ABSTAIN if S < τ"], style="out")
    box(414, 48, 98, 148, [("b", "Evidence verifier"), "deterministic, independent", "",
                           ("L", "V0  schema + unit"), ("L", "V1  evidence ID exists"),
                           ("L", "V2  case + time window"), ("L", "V3  emitter / callee"),
                           ("L", "V4  value + unit"), ("L", "V5  above baseline"),
                           ("L", "V6  observed call edge"), "", ("i", "never changes a ranking")])
    box(414, 202, 98, 24, [("b", "Accepted / rejected claims"), ("i", "not audited truth")], style="out")
    arrow((398, 30), (414, 30))                          # E0 ranking -> abstention (margin)
    arrow((398, 74), (414, 74))                          # E0 template claims
    arrow((336, 128), (414, 128))                        # E1 claims
    arrow((398, 145), (414, 145), dashed=True)           # E2 canonical claims
    arrow((398, 189.5), (414, 189.5))                    # B4 / B4-G claims
    arrow((463, 48), (463, 40))                          # verified-claim coverage -> abstention
    arrow((463, 196), (463, 202))

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
    env = dict(os.environ, SOURCE_DATE_EPOCH="1760572800")   # fixed PDF CreationDate, so rebuilds are byte-identical
    for fmt in ("svg", "pdf"):
        subprocess.run(["neato", "-n2", f"-T{fmt}:cairo" if fmt == "pdf" else f"-T{fmt}", str(src),
                        "-o", str(HERE / f"{NAME}.{fmt}")], check=True, env=env)
    subprocess.run(["neato", "-n2", "-Tpng:cairo", "-Gdpi=300", str(src), "-o", str(HERE / f"{NAME}.png")],
                   check=True, env=env)


if __name__ == "__main__":
    main()
