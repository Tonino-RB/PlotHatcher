"""Substitute glyphs with vpype's built-in single-stroke (Hershey-family) fonts.

Deliberately bypasses vpype's own ``text_line``/``text_block`` — those lay
out a whole string with the Hershey font's *own* advances, which is exactly
what the tool must not do here. Instead this reaches into vpype's internal
``_Font``/``_Glyph`` glyph data (the only per-glyph access vpype offers; no
public API exists for it, so this is inherently coupled to vpype's current
internal representation) and places each substitute glyph at the exact pen
position already computed by HarfBuzz shaping (``shaping.py``) for the
*original* font — same position, same per-run font-size scale, same
document transform — so overall layout matches the source SVG text exactly
while only the glyph's ink is swapped out.

Hershey glyphs are already authored in a y-down convention matching vpype's
(and this project's) document space, unlike TrueType/OpenType outlines, so
no y-flip is needed here (contrast with ``outlines.py``).
"""

from __future__ import annotations

import numpy as np
import svgelements as se
from vpype.text import FONT_NAMES, _Font, _Glyph

from .shaping import ShapedGlyph, ShapedRun

__all__ = ["FONT_NAMES", "hershey_lines_for_glyph"]

_HERSHEY_BASELINE_Y = 9.0
"""Baseline y in the classic Hershey digitization's raw coordinate space.

Not exposed anywhere in vpype's API — verified empirically (the y-bottom of
'x'/'H' glyphs) to be this fixed constant across every font bundled with
vpype (futural, futuram, timesr, timesrb, scriptc, gothiceng, cyrillic, ...),
since they're all digitized on the same classic Hershey grid."""


_LIGATURES: dict[str, str] = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
}
"""Common Latin ligatures some source files (e.g. Affinity Designer's SVG
export) substitute in as a single character. HarfBuzz then shapes that as
one glyph with one cluster, so the plain ord(char) - 32 lookup below would
see a single out-of-table codepoint and silently drop the whole ligature —
both letters at once, not just the merged one. Expanded back to their
letters here and laid out left-to-right, spacing them evenly across the
*original* font's advance for the merged glyph (see ``_ligature_spacing_scale``)
rather than each letter's own natural Hershey width — Hershey's 'f'+'i'
combined is rarely the same width as the source font's actual "fi" advance,
and packing them at their own natural width left 'i' crowded against
whatever comes next instead of evenly spaced between 'f' and it."""


_ROUND_CORNERS_ITERATIONS = 2


def _chaikin_smooth(points: list[complex], iterations: int = _ROUND_CORNERS_ITERATIONS) -> list[complex]:
    """Chaikin corner-cutting: each interior vertex is replaced by two points
    blended 1/4 and 3/4 of the way to its neighbors, which converges to a
    rounded curve after a couple of iterations. Endpoints are kept fixed so
    the substitute glyph's start/end position (and thus its measured
    advance) doesn't drift — only the interior corners round off."""
    pts = points
    for _ in range(iterations):
        if len(pts) < 3:
            break
        new_pts = [pts[0]]
        for i in range(len(pts) - 1):
            p0, p1 = pts[i], pts[i + 1]
            new_pts.append(p0 + 0.25 * (p1 - p0))
            new_pts.append(p0 + 0.75 * (p1 - p0))
        new_pts.append(pts[-1])
        pts = new_pts
    return pts


def _next_glyph_pen_x(shaped_run: ShapedRun, shaped_glyph: ShapedGlyph) -> float | None:
    """Raw pen_x of the glyph right after ``shaped_glyph`` in its run, or
    None if it's the run's last glyph — used to recover how wide the
    *original* font made a merged ligature glyph, since ShapedGlyph itself
    has no advance field."""
    glyphs = shaped_run.glyphs
    for i, g in enumerate(glyphs):
        if g is shaped_glyph:
            return glyphs[i + 1].pen_x if i + 1 < len(glyphs) else None
    return None


def _ligature_spacing_scale(
    shaped_run: ShapedRun, shaped_glyph: ShapedGlyph, font_unit_scale: float, natural_total: float
) -> float:
    """Factor to stretch/shrink the natural gaps between a ligature's
    letters so they exactly span the original font's advance for the
    merged glyph, instead of overflowing into (or leaving a gap before)
    whatever comes next. 1.0 (no change) if that advance can't be
    recovered (last glyph of a run) or the letters have no natural width."""
    next_pen_x = _next_glyph_pen_x(shaped_run, shaped_glyph)
    if next_pen_x is None or natural_total <= 0:
        return 1.0
    allotted = (next_pen_x - shaped_glyph.pen_x) * font_unit_scale
    return allotted / natural_total if allotted > 0 else 1.0


def hershey_lines_for_glyph(
    shaped_run: ShapedRun,
    shaped_glyph: ShapedGlyph,
    font_name: str,
    round_corners: bool = False,
) -> list[np.ndarray]:
    """Document-space stroke lines substituting one glyph with a Hershey glyph."""
    text = shaped_run.run.text
    if not (0 <= shaped_glyph.cluster < len(text)):
        return []
    char = text[shaped_glyph.cluster]
    chars = _LIGATURES.get(char, char)
    font = _Font.get(font_name)

    upem = shaped_glyph.upem or shaped_run.upem
    scale = shaped_run.run.font_size / font.max_height
    font_unit_scale = shaped_run.run.font_size / upem
    pen_x = (shaped_glyph.pen_x + shaped_glyph.x_offset) * font_unit_scale
    pen_y = (shaped_glyph.pen_y + shaped_glyph.y_offset) * font_unit_scale - _HERSHEY_BASELINE_Y * scale

    resolved: list[tuple[_Glyph, float]] = []
    for c in chars:
        index = ord(c) - 32
        if not (0 <= index < len(font.glyphs)):
            continue
        glyph = font.glyphs[index]
        if len(glyph.lines) == 0:
            continue
        resolved.append((glyph, (glyph.rt - glyph.lt) * scale))
    if not resolved:
        return []

    spacing_scale = 1.0
    if len(resolved) > 1:
        spacing_scale = _ligature_spacing_scale(
            shaped_run, shaped_glyph, font_unit_scale, sum(width for _, width in resolved)
        )

    lines: list[np.ndarray] = []
    advance = 0.0
    for glyph, natural_width in resolved:
        tx = pen_x + advance - glyph.lt * scale
        local = se.Matrix(scale, 0, 0, scale, tx, pen_y)
        combined = local * shaped_run.run.transform

        for line in glyph.lines:
            pts = [se.Point(p.real, p.imag) * combined for p in line]
            complex_pts = [complex(p.x, p.y) for p in pts]
            if round_corners:
                complex_pts = _chaikin_smooth(complex_pts)
            lines.append(np.array(complex_pts))

        advance += natural_width * spacing_scale
    return lines
