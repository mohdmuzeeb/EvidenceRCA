"""Minimal, dependency-free SVG drawing for the paper figures (matplotlib is not installed; nothing is downloaded).

Coordinates are in points (1/72 in). Output is deterministic text, so figure files can be hash-checked.
"""
from __future__ import annotations

from html import escape

FONT = "Helvetica, Arial, sans-serif"
# Okabe-Ito colour-blind-safe palette; every series also gets a distinct marker for greyscale print
COLOR = {"E0": "#0072B2", "E1": "#0072B2", "B1": "#009E73", "B2": "#E69F00", "B3": "#CC79A7",
         "E0-A-graph": "#56B4E9", "B4": "#D55E00", "B4-G": "#8C3B00", "E1-frozen-gated": "#004C77",
         "E2-canonical": "#999999", "grey": "#7F7F7F", "ink": "#000000", "grid": "#D9D9D9"}
MARKER = {"E0": "circle", "E1": "circle", "B1": "triangle", "B2": "square", "B3": "diamond",
          "E0-A-graph": "triangle-down", "B4": "square", "B4-G": "diamond", "E1-frozen-gated": "circle"}


def f(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


class Fig:
    def __init__(self, width_in: float, height_in: float):
        self.w, self.h = width_in * 72, height_in * 72
        self.parts: list[str] = []

    # primitives
    def line(self, x1, y1, x2, y2, color="#000", width=0.6, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<line x1="{f(x1)}" y1="{f(y1)}" x2="{f(x2)}" y2="{f(y2)}" stroke="{color}" '
                          f'stroke-width="{f(width)}"{d}/>')

    def rect(self, x, y, w, h, fill, stroke="none", width=0.5):
        self.parts.append(f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="{f(width)}"/>')

    def polyline(self, pts, color, width=1.0, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        p = " ".join(f"{f(x)},{f(y)}" for x, y in pts)
        self.parts.append(f'<polyline points="{p}" fill="none" stroke="{color}" stroke-width="{f(width)}"{d}/>')

    def text(self, x, y, s, size=7.5, anchor="start", weight="normal", color="#000", rotate=None, italic=False):
        r = f' transform="rotate({rotate} {f(x)} {f(y)})"' if rotate else ""
        st = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{f(x)}" y="{f(y)}" font-family="{FONT}" font-size="{f(size)}" '
                          f'text-anchor="{anchor}" font-weight="{weight}" fill="{color}"{st}{r}>{escape(s)}</text>')

    def marker(self, x, y, kind, color, size=2.6, filled=True):
        fill = color if filled else "#FFFFFF"
        s = size
        if kind == "circle":
            self.parts.append(f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(s)}" fill="{fill}" stroke="{color}" '
                              f'stroke-width="0.8"/>')
            return
        shapes = {"square": [(-s, -s), (s, -s), (s, s), (-s, s)],
                  "diamond": [(0, -1.3 * s), (1.3 * s, 0), (0, 1.3 * s), (-1.3 * s, 0)],
                  "triangle": [(0, -1.3 * s), (1.2 * s, s), (-1.2 * s, s)],
                  "triangle-down": [(0, 1.3 * s), (1.2 * s, -s), (-1.2 * s, -s)]}
        p = " ".join(f"{f(x + dx)},{f(y + dy)}" for dx, dy in shapes[kind])
        self.parts.append(f'<polygon points="{p}" fill="{fill}" stroke="{color}" stroke-width="0.8"/>')

    def svg(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{f(self.w / 72)}in" height="{f(self.h / 72)}in" '
                f'viewBox="0 0 {f(self.w)} {f(self.h)}">\n<rect width="100%" height="100%" fill="#FFFFFF"/>\n'
                + "\n".join(self.parts) + "\n</svg>\n")


class Axes:
    """Maps data to a plot rectangle inside a Fig and draws axes, ticks and grid."""

    def __init__(self, fig: Fig, x0, y0, w, h, xlim, ylim):
        self.fig, self.x0, self.y0, self.w, self.h = fig, x0, y0, w, h
        self.xlim, self.ylim = xlim, ylim

    def X(self, v):
        return self.x0 + (v - self.xlim[0]) / (self.xlim[1] - self.xlim[0]) * self.w

    def Y(self, v):
        return self.y0 + self.h - (v - self.ylim[0]) / (self.ylim[1] - self.ylim[0]) * self.h

    def yaxis(self, ticks, label=None, fmt=lambda v: f"{v:.1f}", grid=True, size=7):
        fg = self.fig
        for t in ticks:
            if grid:
                fg.line(self.x0, self.Y(t), self.x0 + self.w, self.Y(t), COLOR["grid"], 0.4)
            fg.line(self.x0 - 2, self.Y(t), self.x0, self.Y(t))
            fg.text(self.x0 - 3.5, self.Y(t) + 2.4, fmt(t), size=size, anchor="end")
        fg.line(self.x0, self.y0, self.x0, self.y0 + self.h)
        if label:
            fg.text(self.x0 - 24, self.y0 + self.h / 2, label, size=size + 0.5, anchor="middle", rotate=-90)

    def xaxis(self, ticks, labels=None, label=None, fmt=lambda v: f"{v:.1f}", grid=False, size=7):
        fg = self.fig
        yb = self.y0 + self.h
        for i, t in enumerate(ticks):
            if grid:
                fg.line(self.X(t), self.y0, self.X(t), yb, COLOR["grid"], 0.4)
            fg.line(self.X(t), yb, self.X(t), yb + 2)
            lab = labels[i] if labels else fmt(t)
            first, _, second = lab.partition("\n")   # optional second line in a smaller size
            fg.text(self.X(t), yb + 9.5, first, size=size, anchor="middle")
            if second:
                fg.text(self.X(t), yb + 17.5, second, size=size - 0.8, anchor="middle", color="#333333")
        fg.line(self.x0, yb, self.x0 + self.w, yb)
        if label:
            fg.text(self.x0 + self.w / 2, yb + (28 if labels and any("\n" in l for l in labels) else 20), label,
                    size=size + 0.5, anchor="middle")

    def errorbar(self, x, y, lo, hi, color, kind="circle", filled=True, cap=1.6, horizontal=False):
        fg = self.fig
        if horizontal:   # x is the value axis
            fg.line(self.X(lo), self.Y(y), self.X(hi), self.Y(y), color, 0.8)
            for v in (lo, hi):
                fg.line(self.X(v), self.Y(y) - cap, self.X(v), self.Y(y) + cap, color, 0.8)
            fg.marker(self.X(x), self.Y(y), kind, color, filled=filled)
            return
        fg.line(self.X(x), self.Y(lo), self.X(x), self.Y(hi), color, 0.8)
        for v in (lo, hi):
            fg.line(self.X(x) - cap, self.Y(v), self.X(x) + cap, self.Y(v), color, 0.8)
        fg.marker(self.X(x), self.Y(y), kind, color, filled=filled)


def legend(fig: Fig, x, y, entries, size=7, step=None, horizontal=True, gap=10):
    """entries: (label, color, marker or 'patch'). Returns the end x (horizontal) or end y."""
    cx, cy = x, y
    for label, color, kind in entries:
        if kind == "patch":
            fig.rect(cx, cy - 4.5, 7, 6, color, "#000", 0.3)
        elif kind == "line":
            fig.line(cx - 1, cy - 1.5, cx + 9, cy - 1.5, color, 1.2)
        else:
            fig.marker(cx + 3.5, cy - 1.5, kind, color)
        fig.text(cx + 11, cy + 1, label, size=size)
        if horizontal:
            cx += 11 + len(label) * size * 0.52 + gap
        else:
            cy += step or size + 4
    return cx if horizontal else cy
