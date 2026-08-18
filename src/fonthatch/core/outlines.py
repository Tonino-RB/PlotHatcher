"""Turn shaped glyphs into filled shapely polygons in document space.

Each glyph's raw font-unit outline (extracted via a flattening ``BasePen``
subclass, so cubic/quadratic curves — including TrueType's implied-on-curve
quadratics — are handled by fontTools itself rather than hand-rolled) is
positioned by the shaping pen coordinates, scaled to user units, flipped
from the font's y-up convention to SVG's y-down convention, and mapped into
document space through the text block's cumulative transform — all as one
affine matrix per glyph.

Contours are combined with the even-odd rule (via iterated
``symmetric_difference``) rather than trusting winding direction, since
TrueType (clockwise-outer) and PostScript/CFF (counter-clockwise-outer)
fonts use opposite conventions and simple, non-self-overlapping glyph
contours give identical results under even-odd and nonzero-winding rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache, reduce

import svgelements as se
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .shaping import ShapedBlock, ShapedGlyph, ShapedRun
from .svg_text import TextRun

_CURVE_STEPS = 8


@dataclass
class GlyphOutline:
    run: TextRun
    polygon: BaseGeometry | None
    """None (or empty) for whitespace/marks with no ink."""
    shaped_run: ShapedRun
    shaped_glyph: ShapedGlyph
    """Carried through so render.py can build Hershey substitute strokes
    (singleline.py) without re-running shaping for the same glyph."""


@lru_cache(maxsize=None)
def _load_glyph_set(path: str, face_index: int):
    tt = TTFont(path, fontNumber=face_index, lazy=True)
    return tt.getGlyphSet(), tt.getGlyphOrder()


def _cubic_point(p0, p1, p2, p3, t):
    mt = 1 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
    return x, y


class _FlatteningPen(BasePen):
    """Records glyph contours as polylines, transformed into document space."""

    def __init__(self, glyph_set, point_transform):
        super().__init__(glyph_set)
        self._point_transform = point_transform
        self.contours: list[list[tuple[float, float]]] = []
        self._current: list[tuple[float, float]] = []

    def _moveTo(self, pt):
        self._current = [self._point_transform(*pt)]

    def _lineTo(self, pt):
        self._current.append(self._point_transform(*pt))

    def _curveToOne(self, p1, p2, p3):
        p0 = self._current[-1]
        tp1 = self._point_transform(*p1)
        tp2 = self._point_transform(*p2)
        tp3 = self._point_transform(*p3)
        for i in range(1, _CURVE_STEPS + 1):
            self._current.append(_cubic_point(p0, tp1, tp2, tp3, i / _CURVE_STEPS))

    def _closePath(self):
        if len(self._current) >= 3:
            self.contours.append(self._current)
        self._current = []

    def _endPath(self):
        self._closePath()


def _combine_even_odd(contours: list[list[tuple[float, float]]]) -> BaseGeometry | None:
    polys = []
    for contour in contours:
        try:
            p = Polygon(contour)
        except ValueError:
            continue
        if not p.is_valid:
            p = p.buffer(0)
        if not p.is_empty:
            polys.append(p)
    if not polys:
        return None
    return reduce(lambda a, b: a.symmetric_difference(b), polys)


def _glyph_polygon(glyph_id: int, shaped_run: ShapedRun, shaped_glyph: ShapedGlyph) -> BaseGeometry | None:
    font = shaped_glyph.font or shaped_run.font
    upem = shaped_glyph.upem or shaped_run.upem
    glyph_set, glyph_order = _load_glyph_set(font.path, font.face_index)
    glyph_name = glyph_order[glyph_id]

    scale = shaped_run.run.font_size / upem
    # e/f are added post-scale, so they must already be in user units: the
    # glyph origin (pen position) translated by scale, not raw font units.
    tx = (shaped_glyph.pen_x + shaped_glyph.x_offset) * scale
    ty = (shaped_glyph.pen_y + shaped_glyph.y_offset) * scale
    local = se.Matrix(scale, 0, 0, -scale, tx, ty)
    combined = local * shaped_run.run.transform

    def point_transform(x, y):
        p = se.Point(x, y) * combined
        return (p.x, p.y)

    pen = _FlatteningPen(glyph_set, point_transform)
    glyph_set[glyph_name].draw(pen)
    return _combine_even_odd(pen.contours)


def extract_outlines(shaped_block: ShapedBlock) -> list[GlyphOutline]:
    outlines: list[GlyphOutline] = []
    for shaped_run in shaped_block.runs:
        for shaped_glyph in shaped_run.glyphs:
            polygon = _glyph_polygon(shaped_glyph.glyph_id, shaped_run, shaped_glyph)
            outlines.append(
                GlyphOutline(run=shaped_run.run, polygon=polygon, shaped_run=shaped_run, shaped_glyph=shaped_glyph)
            )
    return outlines
