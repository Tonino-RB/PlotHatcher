"""End-to-end orchestration: SVG in -> original document + hatched layers -> SVG out."""

from __future__ import annotations

from .compose import compose_svg_string
from .compose import compose_svg_to_file as _compose_svg_to_file
from .hatch import HatchParams
from .outlines import GlyphOutline, extract_outlines
from .render import RenderMode, RenderParams
from .shaping import shape_block
from .svg_text import extract_text_blocks


def extract_glyph_outlines(input_svg_path: str) -> list[GlyphOutline]:
    outlines: list[GlyphOutline] = []
    for block in extract_text_blocks(input_svg_path):
        outlines.extend(extract_outlines(shape_block(block)))
    return outlines


def _normalize_params(params: RenderParams | HatchParams) -> RenderParams:
    if isinstance(params, HatchParams):
        return RenderParams(mode=RenderMode.HATCH, hatch=params)
    return params


def process_svg_to_string(
    input_svg_path: str,
    params: RenderParams | HatchParams,
    *,
    layer_render_params: dict[int, RenderParams] | None = None,
) -> str:
    render_params = _normalize_params(params)
    glyph_outlines = extract_glyph_outlines(input_svg_path)
    return compose_svg_string(input_svg_path, glyph_outlines, render_params, layer_render_params=layer_render_params)


def process_svg(
    input_svg_path: str,
    output_svg_path: str,
    params: RenderParams | HatchParams,
    *,
    layer_render_params: dict[int, RenderParams] | None = None,
) -> None:
    render_params = _normalize_params(params)
    glyph_outlines = extract_glyph_outlines(input_svg_path)
    _compose_svg_to_file(
        input_svg_path, output_svg_path, glyph_outlines, render_params, layer_render_params=layer_render_params
    )
