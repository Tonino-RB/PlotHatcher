"""Dispatch a glyph to either the hatch engine or the Hershey substitution mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .hatch import ContourMode, HatchParams, hatch_polygon
from .outlines import GlyphOutline
from .singleline import hershey_lines_for_glyph

__all__ = ["RenderMode", "RenderParams", "ContourMode", "render_glyph_lines"]


class RenderMode(str, Enum):
    HATCH = "hatch"
    SINGLELINE = "singleline"


@dataclass
class RenderParams:
    mode: RenderMode = RenderMode.HATCH
    hatch: HatchParams = field(default_factory=HatchParams)
    singleline_font: str = "futural"
    singleline_round_corners: bool = False
    """Smooth the substitute glyph's strokes (Chaikin corner-cutting) for a
    slightly rounded look, rather than the Hershey font's raw straight-line
    segments."""
    draw_contour: bool = True
    """Whether to trace the glyph's true outline at all (HATCH mode only)."""
    draw_hatch: bool = True
    """Whether to fill the glyph with the hatch pattern at all (HATCH mode
    only) — paired with draw_contour so a glyph can be rendered as just its
    outline, just its fill, or both."""
    contour_separate_layer: bool = False
    """Put the contour on its own "contour" layer instead of merging it into "hatched"."""
    contour_mode: ContourMode = ContourMode.OUTER


def render_glyph_lines(glyph: GlyphOutline, params: RenderParams) -> list[np.ndarray]:
    """Content strokes for the "hatched" layer for one glyph (not the outline
    itself — callers add that separately for HATCH mode; SINGLELINE mode has
    no outline of its own, the substitute glyph *is* the content)."""
    if params.mode == RenderMode.SINGLELINE:
        return hershey_lines_for_glyph(
            glyph.shaped_run, glyph.shaped_glyph, params.singleline_font, params.singleline_round_corners
        )
    if params.mode == RenderMode.HATCH:
        if not params.draw_hatch:
            return []
        strokes = hatch_polygon(glyph.polygon, params.hatch)
        return [np.array([complex(x, y) for x, y in s.coords]) for s in strokes]
    raise ValueError(f"Unknown render mode: {params.mode}")
