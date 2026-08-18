"""End-to-end orchestration: SVG in -> text/hatched layers -> SVG out."""

from __future__ import annotations

from pathlib import Path

from .hatch import HatchParams
from .layers import DEFAULT_QUANTIZATION, build_document
from .outlines import GlyphOutline, extract_outlines
from .render import RenderMode, RenderParams
from .shaping import shape_block
from .svg_output import render_svg
from .svg_output import write_svg as _write_svg
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
    quantization: float = DEFAULT_QUANTIZATION,
) -> str:
    render_params = _normalize_params(params)
    glyph_outlines = extract_glyph_outlines(input_svg_path)
    doc, text_id, _hatched_id, _contour_id = build_document(input_svg_path, glyph_outlines, render_params, quantization)
    return render_svg(doc, hidden_layer_ids=[text_id])


def process_svg(
    input_svg_path: str,
    output_svg_path: str,
    params: RenderParams | HatchParams,
    quantization: float = DEFAULT_QUANTIZATION,
) -> None:
    if Path(input_svg_path).resolve() == Path(output_svg_path).resolve():
        raise ValueError(
            f"Refusing to write output over the input file ({input_svg_path}) — "
            "choose a different output path."
        )
    render_params = _normalize_params(params)
    glyph_outlines = extract_glyph_outlines(input_svg_path)
    doc, text_id, _hatched_id, _contour_id = build_document(input_svg_path, glyph_outlines, render_params, quantization)
    _write_svg(doc, output_svg_path, hidden_layer_ids=[text_id])
