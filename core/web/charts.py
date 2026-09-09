# Copyright 2026 Ryan Gomez & Co. Inc. — PHI AI
# Licensed under the Apache License, Version 2.0 (see LICENSE); attribution notices must be retained (see NOTICE).
"""
Inline-SVG charts for the cohort report.

Pure functions: rows in, `markupsafe.Markup` out. No database, no request
object, no script - this interface serves `script-src 'self'` and a chart
that needs a library is a chart that needs a third origin.

ONE PIXEL SCALE. Every chart is drawn at its natural size in CSS pixels
- 600 wide; a bars chart as tall as its rows (30px each), a histogram or
a line 260 tall - and carries explicit width and height attributes, so a
label is 12px on every chart on the page. The stylesheet (app.css,
`.cr-svg`) lets a chart shrink when its card is narrower than 600px and
never lets it grow: a two-row chart is a small drawing, a twelve-row
chart a tall one, and the card around each takes its chart's own height.

The marks follow the mark specs the cohort-report contract pins, which
are the validated reference palette's: the cohort's bar 12px thick and
everyone's 10px with a 2px surface gap between them (24px, the cap), a
4px rounded data end and a square foot on the baseline, 2px lines with
round joins, end markers of at least 8px ringed in the surface colour,
hairline solid gridlines, ONE axis, direct value labels on the cohort
series only, and a hatched stub wherever a count is under 11. Text wears
the page's text tokens (app.css, `.cr-svg`), never a series colour:
identity comes from the mark beside the words. A label that does not fit
its column is cut with an ellipsis and carries the full text in a
<title>; every mark carries a <title>, so native hover explains it
without any script.

PHI never enters these: the inputs are labels and counts.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Optional, Sequence

from markupsafe import Markup, escape

# The contract's colours (a validated reference palette on a light
# surface): the cohort is the one accent, everyone is muted ink.
COHORT = "#2a78d6"
EVERYONE = "#898781"
RAMP = ("#9ec5f4", "#1c5cab")   # histogram hue ramp, light -> dark with magnitude
SERIOUS = "#ec835a"             # status-serious: abnormal share, always with a label
SURFACE = "#ffffff"
SUPPRESSED = "< 11"

SERIES_LABELS = {"cohort": "this cohort", "everyone": "everyone"}
SERIES_COLOURS = {"cohort": COHORT, "everyone": EVERYONE}

# The geometry, in CSS pixels. One scale for every chart on the page.
_VIEW_W = 600       # every chart's natural width
_LABEL_W = 220      # bars: the category label column (a label wraps to two lines)
_VALUE_W = 60       # bars: the direct value label column past the longest bar
_BAR_COHORT = 12    # the cohort's bar
_BAR_EVERYONE = 10  # everyone's bar: the pair and its gap are 24px, the cap
_BAR_SINGLE = 16    # a one-series chart's bar
_GAP = 2            # the surface gap between the two series' bars
_ROW = 30           # one category's row: the 24px pair and 6px of air
_ROW_WRAP = 36      # the row when any label in the chart runs to two lines
_LINE = 14          # the leading between a label's two lines
_HEAD = 34          # the legend line above the first row
_HEAD_BARE = 12     # a one-series chart has no legend
_FOOT = 10
_RADIUS = 4         # the rounded data end
_STUB = 26          # the hatched stub's length for a suppressed cell
_CHART_H = 260      # a histogram's or a line's natural height
_LABEL = 12.0       # px: category labels, legend, direct values (app.css .cr-svg text, .cr-v)
_AXIS = 11.0        # px: ticks and bin labels (.cr-a)

# Per-class advances of the page's sans (Inter, app.css), in em: fitted
# to the widths headless Chrome measured for every label on a rendered
# report, then 6% generous, so a wrap or a cut errs early rather than
# past the column. The estimate only decides where a line breaks and the
# ellipsis goes; the <title> carries the full text either way.
_NARROW = frozenset("iljtfrI.,:;'|!()[]{}/- ·")
_WIDE = frozenset("mwMW@%&…")
_ADVANCE = {"narrow": 0.33, "wide": 0.92, "digit": 0.63, "upper": 0.69, "lower": 0.60}


def slug(text: str) -> str:
    """A stable element id for a chart title."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return f"cr-{s or 'chart'}"


def pct(fraction: Optional[float]) -> str:
    """A share as the label a reader wants: 62%, 4.5%, <1%, 0%."""
    if fraction is None:
        return "—"
    value = float(fraction) * 100
    if value == 0:
        return "0%"
    if value < 1:
        return "<1%"
    if value < 10:
        return f"{value:.1f}%"
    return f"{value:.0f}%"


def fmt(n) -> str:
    """Thousands-separated integers; one decimal otherwise."""
    if n is None:
        return "—"
    if isinstance(n, bool):
        return str(n)
    if isinstance(n, int) or float(n).is_integer():
        return f"{int(n):,}"
    return f"{float(n):,.1f}"


def _text_w(text: str, size: float = _LABEL) -> float:
    """An estimate of a string's rendered width in the page's sans at `size` px."""
    em = 0.0
    for ch in text:
        if ch in _NARROW:
            em += _ADVANCE["narrow"]
        elif ch in _WIDE:
            em += _ADVANCE["wide"]
        elif ch.isdigit():
            em += _ADVANCE["digit"]
        elif ch.isupper():
            em += _ADVANCE["upper"]
        else:
            em += _ADVANCE["lower"]
    return em * size


def _fit(text: str, max_w: float, size: float = _LABEL) -> tuple[str, Optional[str]]:
    """The text as it fits in `max_w` px, and the full text when it had to
    be cut (None when it did not)."""
    if _text_w(text, size) <= max_w:
        return text, None
    keep = text
    while keep and _text_w(keep.rstrip() + "…", size) > max_w:
        keep = keep[:-1]
    return keep.rstrip() + "…", text


def _wrap(text: str, max_w: float, size: float = _LABEL, lines: int = 2) -> tuple[list[str], Optional[str]]:
    """The text on up to `lines` lines of at most `max_w` px, broken at
    spaces; the last line is cut with an ellipsis when the rest does not
    fit, and then the full text comes back too (else None)."""
    if _text_w(text, size) <= max_w:
        return [text], None
    out: list[str] = []
    rest = text.split(" ")
    while rest and len(out) < lines - 1:
        line, k = rest[0], 1
        while k < len(rest) and _text_w(f"{line} {rest[k]}", size) <= max_w:
            line = f"{line} {rest[k]}"
            k += 1
        if _text_w(line, size) > max_w:      # one word wider than the line
            return out + [_fit(line, max_w, size)[0]], text
        out.append(line)
        rest = rest[k:]
    cut, full = _fit(" ".join(rest), max_w, size)
    out.append(cut)
    return out, (text if full else None)


def _mix(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colours, t in [0, 1]."""
    t = min(1.0, max(0.0, t))
    ra, ga, ba = (int(a[i:i + 2], 16) for i in (1, 3, 5))
    rb, gb, bb = (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % (
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def _nice(value: float) -> float:
    """A clean axis ceiling at or above `value`: 1, 2, 2.5, 5 or 10 × 10^k."""
    if value is None or value <= 0:
        return 1.0
    base = 10.0 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if base * step >= value:
            return base * step
    return base * 10


def _ticks(ceiling: float, dense: bool = False) -> list[float]:
    """Clean gridline values from 0 to a _nice() ceiling: the step follows
    the ceiling's leading digit (1 → fifths, 2 → quarters, 2.5 → 0.5
    steps, 5 → fifths) so a tick never reads 33.33; the sparse form keeps
    a histogram at three lines."""
    base = 10.0 ** math.floor(math.log10(ceiling)) if ceiling > 0 else 1.0
    mantissa = round(ceiling / base, 2)
    if dense:
        intervals = 4 if mantissa == 2 else 5
    else:
        intervals = 5 if mantissa == 2.5 else 2
    return [round(ceiling * i / intervals, 6) for i in range(intervals + 1)]


def _tick(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:g}"


def _open(chart_id: str, title: str, height: float, extra_class: str = "") -> str:
    """The root element at its natural size: width and height in CSS px,
    the viewBox the same, so the drawing is 1:1 unless the stylesheet
    has to shrink it."""
    cls = f"cr-svg {extra_class}".strip()
    return (
        f'<svg class="{cls}" role="img" aria-labelledby="{chart_id}-title" '
        f'viewBox="0 0 {_VIEW_W} {height:g}" width="{_VIEW_W}" height="{height:g}">'
        f'<title id="{chart_id}-title">{escape(title)}</title>'
    )


def _hatch(chart_id: str, series: str, colour: str) -> str:
    """The hatched fill for a suppressed cell: 45° tone-on-tone lines."""
    pid = f"{chart_id}-hatch-{series}"
    return (
        f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="6" height="6" '
        f'patternTransform="rotate(45)">'
        f'<rect width="6" height="6" fill="{SURFACE}"/>'
        f'<line x1="0" y1="0" x2="0" y2="6" stroke="{colour}" stroke-width="2"/>'
        f'</pattern>'
    )


def _hbar(x: float, y: float, w: float, t: float, fill: str, title: str,
          cls: str = "cr-mark") -> str:
    """A horizontal bar: square foot at the baseline (x), 4px rounded data end."""
    r = min(_RADIUS, t / 2)
    if w <= r:
        shape = (f'<rect class="{cls}" x="{x:g}" y="{y:g}" width="{max(w, 1):g}" '
                 f'height="{t:g}" fill="{fill}">')
    else:
        d = (f"M{x:g},{y:g} H{x + w - r:g} A{r:g},{r:g} 0 0 1 {x + w:g},{y + r:g} "
             f"V{y + t - r:g} A{r:g},{r:g} 0 0 1 {x + w - r:g},{y + t:g} H{x:g} Z")
        shape = f'<path class="{cls}" d="{d}" fill="{fill}">'
    return f"{shape}<title>{escape(title)}</title></{'rect' if w <= r else 'path'}>"


def _vbar(x: float, ybase: float, w: float, h: float, fill: str, title: str,
          cls: str = "cr-mark") -> str:
    """A column: square foot on the baseline (ybase), 4px rounded cap."""
    r = min(_RADIUS, w / 2)
    if h <= r:
        shape = (f'<rect class="{cls}" x="{x:g}" y="{ybase - max(h, 1):g}" width="{w:g}" '
                 f'height="{max(h, 1):g}" fill="{fill}">')
        tag = "rect"
    else:
        top = ybase - h
        d = (f"M{x:g},{ybase:g} V{top + r:g} A{r:g},{r:g} 0 0 1 {x + r:g},{top:g} "
             f"H{x + w - r:g} A{r:g},{r:g} 0 0 1 {x + w:g},{top + r:g} V{ybase:g} Z")
        shape = f'<path class="{cls}" d="{d}" fill="{fill}">'
        tag = "path"
    return f"{shape}<title>{escape(title)}</title></{tag}>"


def _legend(x: float, y: float, series: Sequence[str]) -> str:
    """'■ this cohort ■ everyone' - the identity channel a reader can rely on."""
    parts = []
    cursor = x
    for s in series:
        colour = SERIES_COLOURS.get(s, EVERYONE)
        label = SERIES_LABELS.get(s, s)
        parts.append(f'<rect x="{cursor:g}" y="{y - 9:g}" width="10" height="10" rx="2" fill="{colour}"/>')
        parts.append(f'<text class="cr-t" x="{cursor + 15:g}" y="{y:g}">{escape(label)}</text>')
        cursor += 15 + _text_w(label) + 18
    return f'<g class="cr-legend">{"".join(parts)}</g>'


def _text(cls: str, x: float, y: float, content: str, anchor: str = "start",
          baseline: str = "", title: Optional[str] = None) -> str:
    """A text run; with `title`, the full text a cut label completes on hover."""
    extra = f' dominant-baseline="{baseline}"' if baseline else ""
    hover = f"<title>{escape(title)}</title>" if title else ""
    return (f'<text class="{cls}" x="{x:g}" y="{y:g}" text-anchor="{anchor}"{extra}>'
            f'{escape(content)}{hover}</text>')


# ---------------------------------------------------------------------------
# bars
# ---------------------------------------------------------------------------

def bars(title: str, rows: Iterable[dict], series: Sequence[str] = ("cohort", "everyone"),
         *, chart_id: Optional[str] = None) -> Markup:
    """Horizontal bars, one 30px row per category, one thin bar per series.

    Each row is a dict with `label`, and per series `s`: `s` (a fraction
    0..1, or None for no mark), `s_text` (the direct label; defaults to
    the share as a percentage), `s_suppressed` (True renders the hatched
    "< 11" stub) and optionally `s_title` (the hover text). The direct
    value label rides the cohort series only; the everyone series is read
    from the legend, the hover and the table view under the chart.

    Natural size: 600 × (34 + 30·rows + 10) with a legend, 600 × (12 +
    30·rows + 10) for one series; the row is 36px when any label in the
    chart wraps to two lines (a label longer than two lines is cut with
    an ellipsis and completed by its <title>). A breakdown with ONE
    category is not a chart - the template says it in a sentence - but
    one row draws.
    """
    rows = list(rows)
    series = tuple(series)
    chart_id = chart_id or slug(title)
    labelled = "cohort" if "cohort" in series else series[0]

    thick = {s: (_BAR_SINGLE if len(series) == 1 else
                 (_BAR_COHORT if s == labelled else _BAR_EVERYONE)) for s in series}
    pair = sum(thick.values()) + _GAP * (len(series) - 1)
    head = _HEAD if len(series) > 1 else _HEAD_BARE
    labels = [_wrap(str(row.get("label", "")), _LABEL_W - 14) for row in rows]
    pitch = _ROW_WRAP if any(len(lines) > 1 for lines, _full in labels) else _ROW
    height = head + max(len(rows), 1) * pitch + _FOOT
    plot_x = _LABEL_W
    plot_w = _VIEW_W - _LABEL_W - _VALUE_W
    ceiling = max((float(r.get(s) or 0) for r in rows for s in series), default=0) or 1.0

    out = [_open(chart_id, title, height, "cr-bars"), "<defs>"]
    for s in series:
        out.append(_hatch(chart_id, s, SERIES_COLOURS.get(s, EVERYONE)))
    out.append("</defs>")
    if len(series) > 1:
        out.append(_legend(plot_x, 14, series))
    # The one axis: the baseline every bar grows from.
    out.append(f'<line class="cr-axis" x1="{plot_x:g}" y1="{head:g}" x2="{plot_x:g}" '
               f'y2="{height - _FOOT + 2:g}" stroke-width="1"/>')
    if not rows:
        out.append(_text("cr-t", plot_x + 8, head + pitch / 2, "nothing recorded", "start", "middle"))
    for i, row in enumerate(rows):
        y = head + i * pitch
        label = str(row.get("label", ""))
        lines, full = labels[i]
        mid = y + pitch / 2
        if len(lines) == 1:
            texts = [_text("cr-t", plot_x - 8, mid, lines[0], "end", "middle")]
        else:
            texts = [_text("cr-t", plot_x - 8, mid - _LINE / 2, lines[0], "end", "middle"),
                     _text("cr-t", plot_x - 8, mid + _LINE / 2, lines[1], "end", "middle")]
        if full:
            out.append(f'<g class="cr-label"><title>{escape(full)}</title>{"".join(texts)}</g>')
        else:
            out.extend(texts)
        by = y + (pitch - pair) / 2
        for s in series:
            t = thick[s]
            value = row.get(s)
            suppressed = bool(row.get(f"{s}_suppressed"))
            text = row.get(f"{s}_text") or (SUPPRESSED if suppressed else pct(value))
            hover = row.get(f"{s}_title") or f"{label} — {SERIES_LABELS.get(s, s)}: {text}"
            colour = SERIES_COLOURS.get(s, EVERYONE)
            width = 0.0
            if suppressed:
                width = _STUB
                out.append(_hbar(plot_x, by, width, t, f"url(#{chart_id}-hatch-{s})",
                                 hover, "cr-mark cr-stub"))
            elif value is not None:
                width = plot_w * float(value) / ceiling if float(value) > 0 else 0.0
                if width:
                    out.append(_hbar(plot_x, by, max(width, 1.0), t, colour, hover))
            if s == labelled:
                out.append(_text("cr-v", plot_x + width + 6, by + t / 2, text, "start", "middle"))
            by += t + _GAP
    out.append("</svg>")
    return Markup("".join(out))


# ---------------------------------------------------------------------------
# hist
# ---------------------------------------------------------------------------

def hist(title: str, bins: Iterable[dict], *, chart_id: Optional[str] = None) -> Markup:
    """Columns for the cohort only: one hue, darker with magnitude. 600 × 260.

    Each bin is a dict with `label`, `n` (a count, or None) and
    `suppressed` (True renders the hatched "< 11" stub). Every column's
    cap carries its count: a histogram has at most ten columns, and the
    count IS the reading. Bin labels wider than their slot go on two
    staggered lines rather than over each other; one wider than two
    slots is cut, with the full text in its <title>.
    """
    bins = list(bins)
    chart_id = chart_id or slug(title)
    left, right, top, height = 50, 12, 24, _CHART_H
    plot_w = _VIEW_W - left - right
    n = max(len(bins), 1)
    slot = plot_w / n
    labels = [str(b.get("label", "")) for b in bins]
    stagger = n > 1 and any(_text_w(lbl, _AXIS) > slot - 6 for lbl in labels)
    bottom = 44 if stagger else 30
    plot_h = height - top - bottom
    ybase = top + plot_h
    col_w = min(24.0, slot * 0.62)
    values = [int(b["n"]) for b in bins if b.get("n") is not None and not b.get("suppressed")]
    peak = max(values) if values else 0
    ceiling = _nice(peak)

    out = [_open(chart_id, title, height, "cr-hist"), "<defs>",
           _hatch(chart_id, "cohort", COHORT), "</defs>"]
    # A few hairline gridlines; the ticks carry what the cap labels do
    # not need to repeat.
    for value in _ticks(ceiling):
        gy = ybase - plot_h * value / ceiling
        out.append(f'<line class="cr-grid" x1="{left:g}" y1="{gy:g}" x2="{left + plot_w:g}" '
                   f'y2="{gy:g}" stroke-width="1"/>')
        out.append(_text("cr-a", left - 6, gy, _tick(value), "end", "middle"))
    if not bins:
        out.append(_text("cr-t", left + 8, top + 14, "nothing recorded"))
    for i, b in enumerate(bins):
        x = left + i * slot + (slot - col_w) / 2
        label = labels[i]
        count = b.get("n")
        if b.get("suppressed"):
            h, text = 14.0, SUPPRESSED
            hover = b.get("title") or f"{label}: {SUPPRESSED}"
            out.append(_vbar(x, ybase, col_w, h, f"url(#{chart_id}-hatch-cohort)", hover,
                             "cr-mark cr-stub"))
        elif count is None:
            h, text = 0.0, "—"
        else:
            count = int(count)
            h = plot_h * count / ceiling if count > 0 else 0.0
            text = fmt(count)
            hover = b.get("title") or f"{label}: {text}"
            if h:
                out.append(_vbar(x, ybase, col_w, h, _mix(RAMP[0], RAMP[1], count / peak if peak else 0),
                                 hover))
        out.append(_text("cr-v", x + col_w / 2, ybase - h - 5, text, "middle"))
        shown, full = _fit(label, (2 * slot if stagger else slot) - 6, _AXIS)
        ly = ybase + 16 + (14 if stagger and i % 2 else 0)
        out.append(_text("cr-a", x + col_w / 2, ly, shown, "middle", title=full))
    out.append("</svg>")
    return Markup("".join(out))


# ---------------------------------------------------------------------------
# line
# ---------------------------------------------------------------------------

def line(title: str, points: Iterable[dict], series: Sequence[str] = ("cohort", "everyone"),
         *, chart_id: Optional[str] = None) -> Markup:
    """2px lines with round joins, ≥ 8px end markers ringed in the surface
    colour, direct end labels, hairline gridlines, one axis - never two.
    600 × 260.

    Each point is a dict with `x` (the label) and per series `s`: a value
    or None (a gap breaks the line), `s_suppressed` (the point is withheld
    - drawn as a gap, "< 11" in the table) and optionally `s_text`.
    """
    points = list(points)
    series = tuple(series)
    chart_id = chart_id or slug(title)
    left, right, top, bottom, height = 50, 118, 30, 26, _CHART_H
    plot_w = _VIEW_W - left - right
    plot_h = height - top - bottom
    ybase = top + plot_h
    values = [float(p[s]) for p in points for s in series
              if p.get(s) is not None and not p.get(f"{s}_suppressed")]
    ceiling = _nice(max(values) if values else 0)
    n = len(points)
    step = plot_w / (n - 1) if n > 1 else 0.0

    def X(i: int) -> float:
        return left + (i * step if n > 1 else plot_w / 2)

    def Y(v: float) -> float:
        return ybase - plot_h * float(v) / ceiling

    out = [_open(chart_id, title, height, "cr-line")]
    if len(series) > 1:
        out.append(_legend(left, 14, series))
    for value in _ticks(ceiling, dense=True):
        gy = ybase - plot_h * value / ceiling
        out.append(f'<line class="cr-grid" x1="{left:g}" y1="{gy:g}" x2="{left + plot_w:g}" '
                   f'y2="{gy:g}" stroke-width="1"/>')
        out.append(_text("cr-a", left - 6, gy, _tick(value), "end", "middle"))
    if n:
        for i in sorted({0, n // 2, n - 1}):
            anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
            out.append(_text("cr-a", X(i), ybase + 16, str(points[i].get("x", "")), anchor))
    else:
        out.append(_text("cr-t", left + 8, top + 14, "nothing recorded"))

    placed: list[float] = []
    for s in series:
        colour = SERIES_COLOURS.get(s, EVERYONE)
        d, pen_down, last = [], False, None
        for i, p in enumerate(points):
            v = p.get(s)
            if v is None or p.get(f"{s}_suppressed"):
                pen_down = False
                continue
            d.append(f"{'L' if pen_down else 'M'}{X(i):g},{Y(v):g}")
            pen_down = True
            last = (i, float(v))
        if d:
            out.append(f'<path class="cr-line" d="{" ".join(d)}" fill="none" stroke="{colour}" '
                       f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round">'
                       f'<title>{escape(SERIES_LABELS.get(s, s))}</title></path>')
        if last is not None:
            i, v = last
            text = points[i].get(f"{s}_text") or fmt(v)
            out.append(f'<circle cx="{X(i):g}" cy="{Y(v):g}" r="6" fill="{SURFACE}"/>')
            out.append(f'<circle class="cr-mark" cx="{X(i):g}" cy="{Y(v):g}" r="4" fill="{colour}">'
                       f'<title>{escape(str(points[i].get("x", "")))} — '
                       f'{escape(SERIES_LABELS.get(s, s))}: {escape(text)}</title></circle>')
            ly = Y(v)
            # End labels that would collide are nudged apart, not stacked.
            while any(abs(ly - other) < 14 for other in placed):
                ly += 14
            placed.append(ly)
            out.append(_text("cr-t", X(i) + 10, ly, f"{SERIES_LABELS.get(s, s)} {text}",
                             "start", "middle"))
    # Native hover per point: an unpainted-looking hit target with a <title>.
    for i, p in enumerate(points):
        for s in series:
            v = p.get(s)
            if p.get(f"{s}_suppressed"):
                text = SUPPRESSED
            elif v is None:
                continue
            else:
                text = p.get(f"{s}_text") or fmt(v)
            cy = Y(v) if v is not None and not p.get(f"{s}_suppressed") else ybase
            out.append(f'<circle class="cr-hit" cx="{X(i):g}" cy="{cy:g}" r="7" fill="transparent">'
                       f'<title>{escape(str(p.get("x", "")))} — {escape(SERIES_LABELS.get(s, s))}: '
                       f'{escape(text)}</title></circle>')
    out.append("</svg>")
    return Markup("".join(out))


# ---------------------------------------------------------------------------
# tile
# ---------------------------------------------------------------------------

def tile(label: str, value, hint: str = "", href: Optional[str] = None) -> Markup:
    """The stat tile: value in tabular figures, label in sentence case, an
    optional hint line. With `href` the value is the link - a total that
    opens to its rows."""
    shown = escape(value)
    if href:
        shown = Markup(f'<a href="{escape(href)}">{shown}</a>')
    hint_html = f'<div class="cr-tile-h">{escape(hint)}</div>' if hint else ""
    return Markup(
        f'<div class="cr-tile"><div class="cr-tile-v">{shown}</div>'
        f'<div class="cr-tile-k">{escape(label)}</div>{hint_html}</div>'
    )
# Made by Ryan Gomez & Co. Inc.
