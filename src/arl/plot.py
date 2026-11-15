"""One metric against the experiment number: as text for a terminal, and as an SVG file.

Nothing here reads the ledger. The command hands in the points.
"""

from __future__ import annotations

import dataclasses
from xml.sax.saxutils import escape

KEPT, SCORED, EARLIER = "kept", "scored", "earlier"
SIGNS = {KEPT: "*", SCORED: "o", EARLIER: "."}
LEGEND = {KEPT: "kept", SCORED: "not kept", EARLIER: "scored under an earlier evaluator"}


@dataclasses.dataclass
class Point:
    n: int  # the experiment's number
    id: str
    value: float
    kind: str  # KEPT, SCORED or EARLIER


def best_so_far(points: list[Point], goal: str | None) -> list[Point]:
    """The kept experiments that were, when they came, the best so far."""
    if goal is None:
        return []
    steps: list[Point] = []
    for p in sorted(points, key=lambda p: p.n):
        if p.kind == KEPT and (not steps or (p.value > steps[-1].value if goal == "max" else p.value < steps[-1].value)):
            steps.append(p)
    return steps


def _span(points: list[Point]) -> tuple[float, float]:
    lo, hi = min(p.value for p in points), max(p.value for p in points)
    if hi == lo:  # one value: give it room, so the axis has two ends
        pad = abs(hi) * 0.05 or 0.5
        lo, hi = lo - pad, hi + pad
    return lo, hi


def _number(value: float) -> str:
    return f"{value:.4g}"


def _every(count: int, room: int) -> int:
    """The step between labelled experiments so that at most `room` of them are labelled: 1, 2, 5, 10, ..."""
    step = 1
    while count / step > max(1, room):
        step = step * 5 // 2 if str(step)[0] == "2" else step * 2
    return step


def caption(points: list[Point], steps: list[Point]) -> str:
    """What the chart says in words: the best kept one and the latest."""
    latest = max(points, key=lambda p: p.n)
    said = [f"latest {latest.id} {_number(latest.value)}"]
    if steps:
        said.insert(0, f"best kept {steps[-1].id} {_number(steps[-1].value)}")
    return ", ".join(said)


def text(points: list[Point], steps: list[Point], title: str, width: int = 100, height: int = 16) -> str:
    """The chart in plain characters. `points` must not be empty."""
    lo, hi = _span(points)
    first, last = min(p.n for p in points), max(p.n for p in points)
    rows = max(4, height)
    count = max(2, rows // 4 + 1)  # labelled rows, spread evenly from the lowest to the highest
    labels = {r: _number(lo + r * (hi - lo) / (rows - 1)) for r in {round(i * (rows - 1) / (count - 1)) for i in range(count)}}
    margin = max(len(s) for s in labels.values()) + 1
    cols = max(12, min(width - margin - 1 - len(str(last)), max(40, 3 * (last - first) + 1)))  # the last label may stand past the axis

    def col(n: int) -> int:
        return round((n - first) * (cols - 1) / max(1, last - first))

    def row(value: float) -> int:
        return round((value - lo) * (rows - 1) / (hi - lo))

    grid = [[" "] * cols for _ in range(rows)]
    for step, after in zip(steps, [*steps[1:], None]):
        end = col(after.n) if after else cols
        for c in range(col(step.n) + 1, end):
            grid[row(step.value)][c] = "-"
    for kind in (EARLIER, SCORED, KEPT):  # the later kinds are drawn over the earlier ones
        for p in points:
            if p.kind == kind:
                grid[row(p.value)][col(p.n)] = SIGNS[kind]

    lines = [title]
    for r in range(rows - 1, -1, -1):
        lines.append(f"{labels.get(r, ''):>{margin}}|{''.join(grid[r]).rstrip()}")
    lines.append(" " * margin + "+" + "-" * cols)
    axis = [" "] * (cols + len(str(last)))
    step = _every(last - first + 1, cols // (len(str(last)) + 2))
    for n in range(first, last + 1):
        if (n - first) % step == 0:
            for i, ch in enumerate(str(n)):
                axis[col(n) + i] = ch
    lines.append(" " * (margin + 1) + "".join(axis).rstrip())
    kinds = [k for k in (KEPT, SCORED, EARLIER) if any(p.kind == k for p in points)]
    legend = [f"{SIGNS[k]} {LEGEND[k]}" for k in kinds] + (["- the best kept so far"] if steps else [])
    lines.append("   ".join(legend))
    return "\n".join(lines) + "\n"


def svg(points: list[Point], steps: list[Point], title: str, name: str) -> str:
    """The same chart as an image. `points` must not be empty."""
    lo, hi = _span(points)
    first, last = min(p.n for p in points), max(p.n for p in points)
    w, h, left, right, top, bottom = 900, 440, 80, 30, 50, 70

    def x(n: float) -> float:
        return left + (n - first) * (w - left - right) / max(1, last - first)

    def y(value: float) -> float:
        return h - bottom - (value - lo) * (h - top - bottom) / (hi - lo)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="13">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<text x="{left}" y="28" font-size="16">{escape(title)}</text>',
    ]
    for i in range(5):
        value = lo + i * (hi - lo) / 4
        out.append(f'<line x1="{left}" y1="{y(value):.1f}" x2="{w - right}" y2="{y(value):.1f}" stroke="#dddddd"/>')
        out.append(f'<text x="{left - 8}" y="{y(value) + 4:.1f}" text-anchor="end">{_number(value)}</text>')
    step = _every(last - first + 1, 20)
    for n in range(first, last + 1):
        if (n - first) % step == 0:
            out.append(f'<line x1="{x(n):.1f}" y1="{h - bottom}" x2="{x(n):.1f}" y2="{h - bottom + 5}" stroke="black"/>')
            out.append(f'<text x="{x(n):.1f}" y="{h - bottom + 20}" text-anchor="middle">{n}</text>')
    out.append(f'<line x1="{left}" y1="{h - bottom}" x2="{w - right}" y2="{h - bottom}" stroke="black"/>')
    out.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{h - bottom}" stroke="black"/>')
    out.append(f'<text x="{(left + w - right) / 2:.0f}" y="{h - bottom + 42}" text-anchor="middle">experiment</text>')
    out.append(f'<text x="18" y="{(top + h - bottom) / 2:.0f}" text-anchor="middle" transform="rotate(-90 18 {(top + h - bottom) / 2:.0f})">{escape(name)}</text>')
    if steps:
        path = f"M {x(steps[0].n):.1f} {y(steps[0].value):.1f}"
        for after in [*steps[1:], None]:
            path += f" H {x(after.n if after else last):.1f}" + (f" V {y(after.value):.1f}" if after else "")
        out.append(f'<path d="{path}" fill="none" stroke="#1f77b4" stroke-width="1.5"/>')
    look = {
        KEPT: 'fill="#1f77b4" stroke="#1f77b4"',
        SCORED: 'fill="white" stroke="#1f77b4"',
        EARLIER: 'fill="#bbbbbb" stroke="#999999"',
    }
    for kind in (EARLIER, SCORED, KEPT):
        for p in points:
            if p.kind == kind:
                out.append(f'<circle cx="{x(p.n):.1f}" cy="{y(p.value):.1f}" r="4.5" {look[kind]}><title>{escape(p.id)} {_number(p.value)}</title></circle>')
    kinds = [k for k in (KEPT, SCORED, EARLIER) if any(p.kind == k for p in points)]
    at = left
    for kind in kinds:
        out.append(f'<circle cx="{at + 5}" cy="{h - 12}" r="4.5" {look[kind]}/><text x="{at + 16}" y="{h - 8}">{LEGEND[kind]}</text>')
        at += 30 + 7 * len(LEGEND[kind])
    if steps:
        out.append(f'<line x1="{at}" y1="{h - 12}" x2="{at + 22}" y2="{h - 12}" stroke="#1f77b4" stroke-width="1.5"/><text x="{at + 28}" y="{h - 8}">the best kept so far</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"
