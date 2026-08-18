"""fonthatch: turn SVG text into pen-plotter hatch-filled outlines.

Given an SVG, the original document — every shape, group, and layer — is
read and written back unchanged. Every original ``<text>`` element is hidden
in place (still real text, never converted to outlines), and for each of the
source file's own top-level layers that has text, a new "<layer> hatched"
layer is added right after it, where each glyph keeps its outline while its
interior is filled with a plotter-style hatch pattern.

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
