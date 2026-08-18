"""fonthatch: turn SVG text into pen-plotter hatch-filled outlines.

Given an SVG, every ``<text>`` element is isolated into a "text" layer,
duplicated into a "hatched" layer (with the "text" layer hidden), and each
glyph in the "hatched" layer keeps its outline while its interior is filled
with a plotter-style hatch pattern.

    from fonthatch import process_svg, HatchParams, FillType

    process_svg("in.svg", "out.svg", HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35))
"""

from .core.accents import marked_font_names
from .core.hatch import FillType, HatchParams, hatch_polygon
from .core.pipeline import extract_glyph_outlines, process_svg, process_svg_to_string
from .core.render import RenderMode, RenderParams

HERSHEY_FONT_NAMES = marked_font_names()
"""Hershey font names, '*'-suffixed where accented glyphs (à/è/é/ë/ù/ç,
upper and lower case) are available — see
fonthatch.core.accents.unmark_font_name."""

__all__ = [
    "FillType",
    "HatchParams",
    "hatch_polygon",
    "RenderMode",
    "RenderParams",
    "HERSHEY_FONT_NAMES",
    "process_svg",
    "process_svg_to_string",
    "extract_glyph_outlines",
]
