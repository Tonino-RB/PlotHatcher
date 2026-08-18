"""Shape TextBlocks with HarfBuzz to get exact per-glyph pen positions.

Positions are computed by walking each block's runs left-to-right, resetting
the cursor on an explicit x/y (SVG "text chunk" semantics) and always adding
dx/dy, then shaping each run's characters with HarfBuzz to get real advances
(including kerning/ligatures where the font provides them) rather than naive
per-character widths. Only a single anchor chunk per block is supported
(anchoring is resolved against the block's first explicit x) — a block with
multiple internal x-resets will anchor as one unit rather than per SVG-spec
sub-chunks; this covers the vast majority of real-world SVG text.

All positions/advances here are in **font units** (unscaled by font-size) in
the text block's local, pre-transform coordinate space. ``outlines.py``
applies the font-size scale, the font's internal y-up-to-SVG-y-down flip,
and the block's document transform in one composed matrix per glyph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import uharfbuzz as hb

from .font_resolve import ResolvedFont, resolve_font, resolve_fallback_font
from .svg_text import TextBlock, TextRun


@dataclass
class ShapedGlyph:
    glyph_id: int
    """Glyph index — in the resolved (original) font, unless ``font`` below
    overrides it, in which case it's a glyph index in *that* font instead."""
    cluster: int
    """Index into the source run's text for the character(s) that produced
    this glyph — used by singleline.py to look up the Hershey substitute."""
    pen_x: float
    """Pen position, in font units of whichever font this glyph actually
    uses (``font or shaped_run.font``) — pre-scale by that font's own upem."""
    pen_y: float
    x_offset: float
    y_offset: float
    font: ResolvedFont | None = None
    """Overrides the containing ``ShapedRun.font`` for this one glyph, when
    the run's own font has no glyph for this character (shaped to
    ``.notdef``, normally a visible tofu box) and a fallback font covering
    it was found — see ``_shape_run``. ``None`` for the overwhelmingly
    common case of a glyph the run's own font covers natively."""
    upem: int | None = None
    """The fallback font's upem, alongside ``font`` above — ``pen_x``/
    ``pen_y`` are in *this* font's units when set, not the run's own."""


@dataclass
class ShapedRun:
    run: TextRun
    font: ResolvedFont
    upem: int
    glyphs: list[ShapedGlyph] = field(default_factory=list)
    end_x: float = 0.0
    """Cursor position (user units) after this run, for flowing the next run."""
    end_y: float = 0.0


@dataclass
class ShapedBlock:
    runs: list[ShapedRun]

    @property
    def transform(self):
        return self.runs[0].run.transform if self.runs else None


@lru_cache(maxsize=None)
def _load_face(path: str, face_index: int) -> tuple[hb.Face, int]:
    blob = hb.Blob.from_file_path(path)
    face = hb.Face(blob, face_index)
    return face, face.upem


def _shape_run(run: TextRun, cursor_x: float, cursor_y: float) -> ShapedRun:
    font_info = resolve_font(run.font_family, run.font_weight, run.font_style)
    face, upem = _load_face(font_info.path, font_info.face_index)

    # A run can be empty but still kept (e.g. a tspan with only an explicit
    # x/y reset and no characters — see svg_text.py). uharfbuzz's own
    # shaping of an empty buffer leaves glyph_positions as None (not []),
    # unlike glyph_infos, so skip HarfBuzz entirely rather than zip() a None.
    if not run.text:
        return ShapedRun(run=run, font=font_info, upem=upem, glyphs=[], end_x=cursor_x, end_y=cursor_y)

    font = hb.Font(face)
    buf = hb.Buffer()
    buf.add_str(run.text)
    buf.guess_segment_properties()
    hb.shape(font, buf)

    glyphs: list[ShapedGlyph] = []
    # Cursor tracked in *user* units (not font units) throughout, since a
    # notdef span rescued by a fallback font (see below) has its own upem —
    # a single font-unit cursor could only ever be consistent with one font.
    ux, uy = cursor_x, cursor_y
    scale = upem / run.font_size
    infos, positions = buf.glyph_infos, buf.glyph_positions
    n = len(infos)
    i = 0
    while i < n:
        info, pos = infos[i], positions[i]
        if info.codepoint != 0:
            glyphs.append(
                ShapedGlyph(
                    glyph_id=info.codepoint,
                    cluster=info.cluster,
                    pen_x=ux * scale,
                    pen_y=uy * scale,
                    x_offset=pos.x_offset,
                    y_offset=pos.y_offset,
                )
            )
            ux += pos.x_advance / scale
            uy += pos.y_advance / scale
            i += 1
            continue

        # A run of one or more consecutive .notdef glyphs (glyph 0 — the
        # resolved font has no mapping for these characters at all). Try to
        # rescue them by re-shaping just this substring with a fallback
        # font that does cover it, rather than emitting the font's (usually
        # visible, box-shaped) .notdef glyph.
        j = i
        while j < n and infos[j].codepoint == 0:
            j += 1
        start_c = min(infos[k].cluster for k in range(i, j))
        end_c = max(infos[k].cluster for k in range(i, j)) + 1
        substring = run.text[start_c:end_c]
        fallback = resolve_fallback_font(ord(substring[0]), run.font_weight, run.font_style) if substring else None

        if fallback is None:
            for k in range(i, j):
                glyphs.append(
                    ShapedGlyph(
                        glyph_id=infos[k].codepoint,
                        cluster=infos[k].cluster,
                        pen_x=ux * scale,
                        pen_y=uy * scale,
                        x_offset=positions[k].x_offset,
                        y_offset=positions[k].y_offset,
                    )
                )
                ux += positions[k].x_advance / scale
                uy += positions[k].y_advance / scale
            i = j
            continue

        fb_face, fb_upem = _load_face(fallback.path, fallback.face_index)
        fb_font = hb.Font(fb_face)
        fb_buf = hb.Buffer()
        fb_buf.add_str(substring)
        fb_buf.guess_segment_properties()
        hb.shape(fb_font, fb_buf)
        fb_scale = fb_upem / run.font_size
        for fb_info, fb_pos in zip(fb_buf.glyph_infos, fb_buf.glyph_positions):
            glyphs.append(
                ShapedGlyph(
                    glyph_id=fb_info.codepoint,
                    cluster=start_c + fb_info.cluster,
                    pen_x=ux * fb_scale,
                    pen_y=uy * fb_scale,
                    x_offset=fb_pos.x_offset,
                    y_offset=fb_pos.y_offset,
                    font=fallback,
                    upem=fb_upem,
                )
            )
            ux += fb_pos.x_advance / fb_scale
            uy += fb_pos.y_advance / fb_scale
        i = j

    return ShapedRun(run=run, font=font_info, upem=upem, glyphs=glyphs, end_x=ux, end_y=uy)


def shape_block(block: TextBlock) -> ShapedBlock:
    cursor_x = 0.0
    cursor_y = 0.0
    chunk_start_x: float | None = None
    shaped_runs: list[ShapedRun] = []

    for run in block.runs:
        if run.x is not None:
            cursor_x = run.x
        if run.y is not None:
            cursor_y = run.y
        cursor_x += run.dx
        cursor_y += run.dy
        if chunk_start_x is None:
            chunk_start_x = cursor_x

        shaped = _shape_run(run, cursor_x, cursor_y)
        shaped_runs.append(shaped)
        cursor_x = shaped.end_x
        cursor_y = shaped.end_y

    total_width = cursor_x - (chunk_start_x or 0.0)
    anchor = block.runs[0].text_anchor if block.runs else "start"
    if anchor == "middle":
        shift = -total_width / 2
    elif anchor == "end":
        shift = -total_width
    else:
        shift = 0.0

    if shift:
        for shaped in shaped_runs:
            font_units_per_user_unit = shaped.upem / shaped.run.font_size
            shift_font_units = shift * font_units_per_user_unit
            for glyph in shaped.glyphs:
                glyph.pen_x += shift_font_units

    return ShapedBlock(runs=shaped_runs)
