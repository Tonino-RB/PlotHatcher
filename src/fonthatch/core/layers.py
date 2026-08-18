"""Assemble the output vpype.Document: original content plus "text"/"hatched"/"contour" layers.

Non-text content is read straight through with vpype's own multilayer SVG
reader (which already ignores ``<text>``, so nothing extra is needed here).
Fresh layer ids are obtained via ``Document.free_id()`` *after* the
passthrough read, so they never collide with layers already present in the
source SVG.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import vpype
from shapely.geometry.base import BaseGeometry

from .hatch import ContourMode, FillType, contour_geometry, iter_polygons
from .outlines import GlyphOutline
from .render import RenderMode, RenderParams, render_glyph_lines

DEFAULT_QUANTIZATION = vpype.convert_length("0.1mm")

TEXT_LAYER_NAME = "text"
HATCHED_LAYER_NAME = "hatched"
CONTOUR_LAYER_NAME = "contour"

_MAX_WORKERS = min(4, os.cpu_count() or 1)
"""Glyphs are independent, and shapely's buffer/union calls (the dominant
cost of hatching, see hatch.py) release the GIL, so rendering them on a
thread pool is a genuine wall-clock win rather than just Python-level
overhead — benchmarked at ~2.3x on an 8-core machine. Higher worker counts
regressed (more contention on the parts that stay in pure Python, e.g. the
ring/row chain-merging in hatch.py), so 4 is the measured sweet spot rather
than "as many as the machine has"."""


def _coords_to_line(coords) -> np.ndarray:
    return np.array([complex(x, y) for x, y in coords])


def _outline_lines(polygon: BaseGeometry) -> list[np.ndarray]:
    lines = []
    for p in iter_polygons(polygon):
        lines.append(_coords_to_line(p.exterior.coords))
        for interior in p.interiors:
            lines.append(_coords_to_line(interior.coords))
    return lines


_GlyphRenderResult = tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]


def _render_one_glyph(glyph: GlyphOutline, glyph_params: RenderParams) -> _GlyphRenderResult:
    """(text_lines, contour_lines, hatched_lines) for one glyph — the unit of
    work parallelized across ``_MAX_WORKERS`` threads by
    :func:`add_text_hatched_layers`."""
    text_lines: list[np.ndarray] = []
    contour_lines: list[np.ndarray] = []

    if glyph.polygon is not None and not glyph.polygon.is_empty:
        text_lines = _outline_lines(glyph.polygon)

        if glyph_params.mode == RenderMode.HATCH and glyph_params.draw_contour:
            # glyph_fill's own fill strokes start one ring past the contour
            # on the assumption the contour was OUTER-traced (see
            # FillType.GLYPH_FILL docstring in hatch.py) — force it here
            # rather than relying on the caller to also set contour_mode, so
            # the two can't drift out of sync.
            effective_contour_mode = (
                ContourMode.OUTER
                if glyph_params.hatch.fill_type == FillType.GLYPH_FILL
                else glyph_params.contour_mode
            )
            contour_geom = contour_geometry(glyph.polygon, effective_contour_mode, glyph_params.hatch.pen_width)
            contour_lines = _outline_lines(contour_geom) if contour_geom is not None else []

    hatched_lines = render_glyph_lines(glyph, glyph_params)
    return text_lines, contour_lines, hatched_lines


def _render_cache_key(params: RenderParams) -> tuple:
    """Hashable summary of every ``RenderParams`` field ``_render_one_glyph``'s
    output actually depends on (everything except which document layer its
    contour ends up in, a document-wide choice made by the caller, not a
    per-glyph one) — used to key ``render_cache`` entries. A plain tuple
    rather than making ``RenderParams``/``HatchParams`` themselves hashable,
    since both are mutated in place by existing callers (e.g. the CLI builds
    one and flips fields on it) and a frozen dataclass would break that."""
    h = params.hatch
    return (
        params.mode,
        h.fill_type,
        h.spacing,
        h.fill_spacing,
        h.inset,
        h.angle,
        h.pen_width,
        h.merge_ends,
        h.zigzag_passes,
        h.merge_tolerance,
        params.singleline_font,
        params.singleline_round_corners,
        params.draw_contour,
        params.draw_hatch,
        params.contour_mode,
    )


def add_text_hatched_layers(
    document: vpype.Document,
    glyph_outlines: list[GlyphOutline],
    render_params: RenderParams,
    *,
    overrides: dict[int, RenderParams] | None = None,
    render_cache: dict[tuple, _GlyphRenderResult] | None = None,
    include_text_layer: bool = True,
) -> tuple[int | None, int, int | None]:
    """Add "text" (hidden-by-caller), "hatched", and optionally "contour"
    layers to an existing Document in place. Returns
    ``(text_layer_id_or_None, hatched_layer_id, contour_layer_id_or_None)``.

    ``include_text_layer=False`` skips adding the "text" layer entirely
    (``text_layer_id`` comes back ``None``) — used by ``compose.py``, which
    keeps the *original* ``<text>`` elements instead of this outline-derived
    stand-in (see its module docstring). ``build_document``'s callers (the
    GUI's live preview, the vpype plugin) still want it: vsketch/vpype can
    only render line geometry, not real SVG text, so the preview needs an
    outline substitute to show anything at all.

    The "text" layer always holds the *original* glyph outline, regardless
    of render mode or contour options — it's the unmodified reference copy.
    In HATCH mode, if ``draw_contour`` is set (the default), the glyph's
    outline — traced either along its true boundary (``ContourMode.
    CENTERLINE``) or offset inward so the pen's outer edge sits on that
    boundary (``ContourMode.OUTER``) — is added either into "hatched"
    alongside the fill, or onto its own "contour" layer if
    ``contour_separate_layer`` is set. SINGLELINE mode never draws a
    contour: the substitute Hershey strokes fully replace the glyph's ink.

    ``overrides``, if given, maps ``id(glyph)`` (identity of one of the
    ``GlyphOutline`` instances in ``glyph_outlines``) to a ``RenderParams``
    used instead of ``render_params`` for that glyph's fill/contour content.
    Keyed by glyph identity rather than e.g. ``block_index`` so this stays
    agnostic of *why* glyphs are grouped — the GUI resolves each glyph's
    RenderParams however it likes (by text, by layer, ...) and just hands
    over the per-glyph result; a glyph missing from the mapping (or when
    ``overrides`` is ``None`` altogether) uses ``render_params``. Structural,
    layer-wide decisions — whether a separate "contour" layer exists at all,
    and the "hatched"/"contour" layers' pen-width metadata — are governed by
    ``render_params`` alone regardless of ``overrides``, since a vpype layer
    can't have two of those at once. CLI/library callers never pass
    ``overrides``, so their behavior is unchanged.

    Used both by :func:`build_document` (fresh read from disk) and by the
    vpype plugin command (document already built by an upstream `read` in a
    `vpype` pipeline — its non-text content must be left untouched).

    ``render_cache``, if given, memoizes each glyph's rendered result by
    ``(id(glyph), its effective params)`` and is mutated in place with any
    freshly-computed entries. Only glyphs whose effective params actually
    differ from a cached entry are recomputed — everything else is reused
    as-is. This is what makes the interactive GUI's redraw-on-every-keystroke
    loop viable on anything beyond a few words: without it, editing one
    text's pen width recomputes the *entire* document's hatch fill every
    time, even though only that text's glyphs changed. Callers that don't
    care about repeated redraws of the same (mostly-unchanged) document —
    the CLI, the vpype plugin, one-shot library use — simply omit it and get
    the previous always-recompute behavior; the cache is the GUI's own
    concern, not a general property of this function. Keyed by glyph
    *identity* rather than content, so it's only safe to reuse across calls
    that share the same ``glyph_outlines`` list (true for the GUI, which
    only ever rebuilds that list when the input file's mtime changes — see
    ``_cached_glyph_outlines`` — and clears its render cache in lockstep)."""
    text_lc = vpype.LineCollection()
    hatched_lc = vpype.LineCollection()
    want_contour_layer = render_params.mode == RenderMode.HATCH and (
        render_params.draw_contour and render_params.contour_separate_layer
    )
    contour_lc = vpype.LineCollection() if want_contour_layer else None

    glyph_params_list = [
        render_params if overrides is None else overrides.get(id(glyph), render_params) for glyph in glyph_outlines
    ]

    if render_cache is None:
        results: list[_GlyphRenderResult | None] = [None] * len(glyph_outlines)
        pending = list(range(len(glyph_outlines)))
        cache_keys: list[tuple] = []
    else:
        cache_keys = [(id(glyph), _render_cache_key(params)) for glyph, params in zip(glyph_outlines, glyph_params_list)]
        results = [render_cache.get(key) for key in cache_keys]
        pending = [i for i, result in enumerate(results) if result is None]

    if pending:
        # Glyphs are independent, so their (expensive, shapely-heavy)
        # rendering is dispatched across a thread pool rather than looped in
        # series. executor.map preserves input order, so results line back
        # up with `pending` regardless of which worker finished first.
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            computed = executor.map(_render_one_glyph, (glyph_outlines[i] for i in pending), (glyph_params_list[i] for i in pending))
            for i, result in zip(pending, computed):
                results[i] = result
                if render_cache is not None:
                    render_cache[cache_keys[i]] = result

    for text_lines, contour_lines, hatched_lines in results:
        text_lc.extend(text_lines)
        if contour_lines:
            (contour_lc if contour_lc is not None else hatched_lc).extend(contour_lines)
        hatched_lc.extend(hatched_lines)

    text_id = None
    if include_text_layer:
        text_id = document.free_id()
        document.add(text_lc, layer_id=text_id)
        document.layers[text_id].set_property(vpype.METADATA_FIELD_NAME, TEXT_LAYER_NAME)

    hatched_id = document.free_id()
    document.add(hatched_lc, layer_id=hatched_id)
    document.layers[hatched_id].set_property(vpype.METADATA_FIELD_NAME, HATCHED_LAYER_NAME)
    # Drives both the exported SVG's stroke-width and, in the GUI, the live
    # preview's rendered line thickness (vpype_viewer reads this same
    # property in its "preview" view mode) — pen_width represents the
    # physical pen regardless of fill mode, so it applies in SINGLELINE too.
    document.layers[hatched_id].set_property(vpype.METADATA_FIELD_PEN_WIDTH, render_params.hatch.pen_width)

    contour_id = None
    if contour_lc is not None:
        contour_id = document.free_id()
        document.add(contour_lc, layer_id=contour_id)
        document.layers[contour_id].set_property(vpype.METADATA_FIELD_NAME, CONTOUR_LAYER_NAME)
        document.layers[contour_id].set_property(vpype.METADATA_FIELD_PEN_WIDTH, render_params.hatch.pen_width)

    return text_id, hatched_id, contour_id


def clone_document(source: vpype.Document) -> vpype.Document:
    """Deep copy of `source`'s layers (content *and* metadata — name, color,
    pen width, ...) into a fresh Document, along with its own metadata/page
    size. ``LineCollection.append()`` always copies each line's coordinate
    array into a new ``np.array(...)`` regardless of what it's given (see
    vpype's own source), so a layer added via ``Document.add(lc, ...,
    with_metadata=True)`` shares no mutable state with `lc` — safe to mutate
    the clone (e.g. adding fresh layers) without affecting `source`. Used by
    `build_document`'s `base_document` to reuse an already-parsed SVG instead
    of re-reading it from disk on every call."""
    doc = vpype.Document(metadata=source.metadata, page_size=source.page_size)
    for layer_id, lc in source.layers.items():
        doc.add(lc, layer_id=layer_id, with_metadata=True)
    return doc


def build_document(
    input_svg_path: str,
    glyph_outlines: list[GlyphOutline],
    render_params: RenderParams,
    quantization: float = DEFAULT_QUANTIZATION,
    *,
    overrides: dict[int, RenderParams] | None = None,
    render_cache: dict[tuple, _GlyphRenderResult] | None = None,
    base_document: vpype.Document | None = None,
) -> tuple[vpype.Document, int, int, int | None]:
    """Returns ``(document, text_layer_id, hatched_layer_id, contour_layer_id_or_None)``.
    See :func:`add_text_hatched_layers` for ``overrides``/``render_cache``.

    ``base_document``, if given, is deep-copied (via :func:`clone_document`)
    instead of re-reading `input_svg_path` from disk — `quantization` is then
    ignored, since it only affects that read. The GUI's live preview passes
    its own (path, mtime)-cached parse here (see sketch.py's
    ``_cached_base_document``): without it, every redraw — including one
    triggered by nothing but a hatch-param tweak — re-parsed the whole SVG's
    non-text content (paths, curves, images, ...) from scratch, which for a
    file with much non-text content dominated redraw time regardless of how
    cheap `render_cache` had made the actual hatching. CLI/library/vpype-
    plugin callers never pass it, so their one-shot behavior is unchanged."""
    doc = clone_document(base_document) if base_document is not None else vpype.read_multilayer_svg(
        input_svg_path, quantization=quantization
    )
    text_id, hatched_id, contour_id = add_text_hatched_layers(
        doc, glyph_outlines, render_params, overrides=overrides, render_cache=render_cache
    )
    return doc, text_id, hatched_id, contour_id
