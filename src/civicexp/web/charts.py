"""Charts, rendered on the server as SVG.

Two forms cover what a program manager needs to see:

* **An effect with its uncertainty**, against zero and against the bar the
  program set before launch. One row per measure or per group. The point
  is the estimate; the line is the range of plausible effects. Whether the
  whole line clears the bar *is* the decision, so the bar is drawn.
* **A meter** for progress toward the planned pilot size.

Both follow the same rules as the rest of the interface: a single series
colour, hairline axes, status carried by labelled chips beside the chart
rather than by colour, and every value also present in a table next to it.
Marks are keyboard-focusable and carry their value for the tooltip and for
screen readers. Colours come from the stylesheet's tokens, so dark mode is
handled there.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .html import Markup, esc, join, render

__all__ = ["Interval", "interval_chart", "meter", "format_value"]


@dataclass(frozen=True)
class Interval:
    label: str
    estimate: float
    low: float
    high: float


def format_value(value: float, unit: str, *, signed: bool = True) -> str:
    """Compact display of an effect in percentage points or time."""
    sign = "+" if signed and value > 0 else ("−" if value < 0 else "")
    magnitude = abs(value)
    if unit == "pts":
        return f"{sign}{magnitude * 100:.1f} pts"
    if magnitude >= 86400:
        return f"{sign}{magnitude / 86400:.1f} d"
    if magnitude >= 3600:
        return f"{sign}{magnitude / 3600:.1f} h"
    if magnitude >= 120:
        return f"{sign}{magnitude / 60:.1f} min"
    return f"{sign}{magnitude:.0f} s"


def _nice_step(span: float, target_ticks: int = 4) -> float:
    raw = span / max(target_ticks, 1)
    if raw <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * power:
            return multiple * power
    return 10 * power


def interval_chart(
    rows: Sequence[Interval],
    *,
    unit: str,
    title: str,
    threshold: float | None = None,
    threshold_label: str = "",
    zero_label: str = "No change",
) -> Markup:
    """A dot-and-range chart, one row per interval."""
    if not rows:
        return Markup("")
    left, right, top, row_h = 168, 612, 34, 34
    width = 640
    height = top + row_h * len(rows) + 30

    values = [0.0]
    for row in rows:
        values.extend((row.low, row.high, row.estimate))
    if threshold is not None:
        values.append(threshold)
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    step = _nice_step(hi - lo)
    lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step

    def x(value: float) -> float:
        return left + (value - lo) / (hi - lo) * (right - left)

    parts: list[Markup] = []
    # Gridlines and tick labels: hairline, solid, recessive.
    tick = lo
    while tick <= hi + step / 2:
        tx = x(tick)
        parts.append(render(
            '<line class="grid" x1="{x}" x2="{x}" y1="{t}" y2="{b}"/>'
            '<text class="tick" x="{x}" y="{ty}" text-anchor="middle">{label}</text>',
            x=f"{tx:.1f}", t=top - 8, b=height - 26, ty=height - 10,
            label=format_value(tick, unit),
        ))
        tick += step
    # Zero: where "no change" sits.
    zx = x(0.0)
    parts.append(render(
        '<line class="zero" x1="{x}" x2="{x}" y1="{t}" y2="{b}"/>'
        '<text class="ref-label" x="{x}" y="{ly}" text-anchor="middle">{label}</text>',
        x=f"{zx:.1f}", t=top - 8, b=height - 26, ly=top - 14, label=zero_label,
    ))
    # The bar the program set before launch.
    if threshold is not None:
        bx = x(threshold)
        anchor = "start" if bx < zx else "end"
        if abs(bx - zx) < 90:
            anchor = "start" if bx >= zx else "end"
        parts.append(render(
            '<line class="threshold" x1="{x}" x2="{x}" y1="{t}" y2="{b}"/>'
            '<text class="ref-label ref-threshold" x="{lx}" y="{ly}" text-anchor="{a}">'
            "{label}</text>",
            x=f"{bx:.1f}", t=top - 8, b=height - 26, ly=top - 14,
            lx=f"{bx + (4 if anchor == 'start' else -4):.1f}", a=anchor,
            label=threshold_label,
        ))

    for index, row in enumerate(rows):
        cy = top + row_h * index + row_h / 2
        tip = (
            f"{row.label}: {format_value(row.estimate, unit)} "
            f"(plausible range {format_value(row.low, unit)} to "
            f"{format_value(row.high, unit)})"
        )
        x1, x2, xe = x(row.low), x(row.high), x(row.estimate)
        parts.append(render(
            '<g class="mark" tabindex="0" role="img" aria-label="{tip}" data-tip="{tip}">'
            '<rect class="hit" x="0" y="{hy}" width="{w}" height="{hh}"/>'
            '<text class="row-label" x="{lx}" y="{ty}" text-anchor="end">{label}</text>'
            '<line class="mark-line" x1="{x1}" x2="{x2}" y1="{cy}" y2="{cy}"/>'
            '<circle class="mark-dot" cx="{xe}" cy="{cy}" r="5"/>'
            '<text class="value-label" x="{vx}" y="{ty}">{value}</text>'
            "</g>",
            tip=tip, hy=f"{cy - row_h / 2:.1f}", w=width, hh=row_h,
            lx=left - 12, ty=f"{cy + 4:.1f}", label=_shorten(row.label, 24),
            x1=f"{x1:.1f}", x2=f"{x2:.1f}", cy=f"{cy:.1f}", xe=f"{xe:.1f}",
            vx=f"{min(x2 + 8, right - 4):.1f}", value=format_value(row.estimate, unit),
        ))

    summary = "; ".join(
        f"{r.label} {format_value(r.estimate, unit)} "
        f"({format_value(r.low, unit)} to {format_value(r.high, unit)})"
        for r in rows
    )
    return render(
        '<figure class="viz"><svg viewBox="0 0 {w} {h}" width="100%" role="group" '
        'aria-label="{title}. {summary}" preserveAspectRatio="xMinYMin meet">'
        "<title>{title}</title>{body}</svg></figure>",
        w=width, h=height, title=title, summary=summary, body=join(parts),
    )


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def meter(fraction: float | None, *, label: str) -> Markup:
    """Progress toward a target. Overshoot is shown as full, and stated in text."""
    if fraction is None:
        return Markup("")
    shown = max(0.0, min(fraction, 1.0))
    width = 300
    fill = max(shown * width, 0.0)
    return render(
        '<svg class="meter" viewBox="0 0 {w} 10" width="100%" height="10" '
        'role="img" aria-label="{label}" preserveAspectRatio="none">'
        '<rect class="meter-track" x="0" y="0" width="{w}" height="10" rx="5"/>'
        '<rect class="meter-fill" x="0" y="0" width="{f}" height="10" rx="5"/></svg>',
        w=width, f=f"{fill:.1f}", label=label,
    )


def legend_note(text: str) -> Markup:
    return render('<p class="viz-note">{t}</p>', t=esc(text))
