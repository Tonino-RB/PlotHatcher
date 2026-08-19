"""Compose the final output SVG by grafting new hatched/contour layers onto
an untouched copy of the original document tree.

Unlike the vpype-based path in layers.py/svg_output.py (still used for the
GUI's interactive preview, see ``layers.build_document`` — vsketch/vpype can
only render line geometry, so the preview has no way to show real SVG
content at all), this never runs the source SVG through vpype's own
reader/writer. Every element the original author wrote — shapes, gradients,
clip-paths, nested groups, per-element styling, the document's own layer
structure and labels — is carried through completely unchanged. Only two
kinds of surgery happen, against a freshly parsed copy of that tree:

1. Every original ``<text>`` element gets ``display:none`` added to its
   style, in place — never moved, never restyled otherwise — so it stays
   exactly where it was, in whatever layer it was already in, just invisible
   by default (toggle it back on in Inkscape's XML editor / Illustrator's
   Layers panel if you want it back).
2. For each of the source SVG's own top-level layers that contains text, one
   new sibling layer is appended immediately after it: "<label> hatched"
   (and, if requested, "<label> contour"), holding only that source layer's
   own hatch/contour geometry. Each is a normal top-level ``<g>`` — a real,
   independently-toggleable layer in Inkscape, Illustrator, Affinity and
   Figma, all of which key their Layers/Objects panel off top-level groups
   — so one source layer's hatch output can be hidden without touching any
   other layer's. A layer with no text of its own gets no hatched/contour
   counterpart at all.

The hatch/contour geometry itself is produced exactly as before (outlines.py
-> hatch.py -> render.py, via ``layers.add_text_hatched_layers``), just
funneled through a throwaway single-layer-worth-of-content vpype.Document
purely to reuse its SVG serialization (path/polyline emission, per-layer
stroke/stroke-width) — that Document's own outer ``<svg>``/viewBox is
discarded; only the inner ``<g>`` is kept and re-parented. Its coordinates
come out in the same "document px" space extract_outlines already places
glyphs in (see svg_text.root_viewport_matrix), which only equals the
original SVG's own viewBox-unit space when width/height and viewBox agree
1:1 — so the grafted ``<g>`` carries a ``transform`` undoing that mapping
whenever it doesn't, keeping new geometry aligned with the original text it
replaces without needing to touch any of the original document's own
coordinates.
"""

from __future__ import annotations

import io
from pathlib import Path

import vpype
from lxml import etree

from .layers import add_text_hatched_layers
from .outlines import GlyphOutline
from .render import RenderParams
from .svg_output import set_display_none
from .svg_text import _INKSCAPE_LABEL, _local_name, root_viewport_matrix, toplevel_layer_ids, toplevel_layer_labels

__all__ = ["compose_svg_string", "compose_svg"]


def _hide_text_elements(root: etree._Element) -> None:
    for el in root.iter():
        if _local_name(el.tag) == "text":
            set_display_none(el)


def _identity(matrix) -> bool:
    return (matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f) == (1, 0, 0, 1, 0, 0)


def _matrix_transform_attr(matrix) -> str | None:
    """``matrix``'s inverse as an SVG ``transform`` value, or ``None`` if
    it's the identity (no viewBox, or a viewBox that already matches
    document-px 1:1) — in which case grafted geometry needs no correction at
    all and adding a no-op ``transform="matrix(1,0,0,1,0,0)"`` would just be
    clutter."""
    if _identity(matrix):
        return None
    inv = ~matrix
    return f"matrix({inv.a},{inv.b},{inv.c},{inv.d},{inv.e},{inv.f})"


def _unique_id(used_ids: set[str], base: str) -> str:
    if base not in used_ids:
        used_ids.add(base)
        return base
    n = 2
    while f"{base}-{n}" in used_ids:
        n += 1
    candidate = f"{base}-{n}"
    used_ids.add(candidate)
    return candidate


def _find_by_id(root: etree._Element, target_id: str) -> etree._Element | None:
    for el in root.iter():
        if el.get("id") == target_id:
            return el
    return None


def _build_layer_fragment(
    glyph_outlines: list[GlyphOutline],
    render_params: RenderParams,
    *,
    overrides: dict[int, RenderParams] | None,
    render_cache: dict[tuple, tuple] | None,
) -> tuple[etree._Element | None, etree._Element | None]:
    """Renders one source layer's worth of glyphs through the existing
    hatch/contour pipeline and returns the resulting ``<g>`` element(s)
    (hatched, contour-or-None), detached from any tree — ready to be
    re-parented wherever the caller likes. Reuses vpype's own SVG writer
    purely for its path/polyline + stroke-width serialization; the tiny
    Document's own page size is irrelevant to the caller (only the inner
    ``<g>`` survives, see module docstring) but must still be set to
    *something* non-``(0, 0)`` — left unset, ``vpype.write_svg`` treats it as
    "tight fit" and translates every coordinate so the geometry's own
    bounding box starts at (0, 0), which would silently shift the grafted
    layer out of alignment with the original text it's meant to sit under."""
    doc = vpype.Document()
    doc.page_size = (1.0, 1.0)
    _text_id, hatched_id, contour_id = add_text_hatched_layers(
        doc,
        glyph_outlines,
        render_params,
        overrides=overrides,
        render_cache=render_cache,
        include_text_layer=False,
    )
    buf = io.StringIO()
    vpype.write_svg(buf, doc)
    frag_root = etree.fromstring(buf.getvalue().encode("utf-8"))

    hatched_el = _find_by_id(frag_root, f"layer{hatched_id}")
    if hatched_el is not None:
        hatched_el.getparent().remove(hatched_el)

    contour_el = _find_by_id(frag_root, f"layer{contour_id}") if contour_id is not None else None
    if contour_el is not None:
        contour_el.getparent().remove(contour_el)

    return hatched_el, contour_el


def _finalize_layer_group(el: etree._Element, used_ids: set[str], base_id: str, label: str, transform: str | None) -> None:
    el.set("id", _unique_id(used_ids, base_id))
    el.set(_INKSCAPE_LABEL, label)
    if transform:
        el.set("transform", transform)
    title = etree.Element("title")
    title.text = label
    el.insert(0, title)


def compose_svg(
    input_svg_path: str,
    glyph_outlines: list[GlyphOutline],
    render_params: RenderParams,
    *,
    layer_render_params: dict[int, RenderParams] | None = None,
    render_cache: dict[tuple, tuple] | None = None,
    hide_original_text: bool = True,
) -> etree._Element:
    """Returns the fully composed output document root (see module
    docstring). ``glyph_outlines`` are bucketed by ``run.layer_index``
    (which of the source SVG's own top-level layers each came from — see
    ``svg_text.toplevel_layer_ids``); each bucket becomes its own
    hatched/contour layer pair, rendered with ``layer_render_params``'s
    entry for that layer id if present, else ``render_params`` — so layers
    can be hatched "all together" (the common case: omit
    ``layer_render_params`` or leave a given layer out of it) or
    independently (give that layer id its own entry)."""
    root = etree.parse(input_svg_path).getroot()
    if hide_original_text:
        _hide_text_elements(root)

    viewport_matrix, _width, _height = root_viewport_matrix(root)
    transform = _matrix_transform_attr(viewport_matrix)

    layer_ids = toplevel_layer_ids(root)
    labels = toplevel_layer_labels(root)
    last_element_for_layer: dict[int, etree._Element] = dict(zip(layer_ids, root))

    by_layer: dict[int, list[GlyphOutline]] = {}
    for glyph in glyph_outlines:
        by_layer.setdefault(glyph.run.layer_index, []).append(glyph)

    used_ids = {el.get("id") for el in root.iter() if el.get("id")}
    layer_render_params = layer_render_params or {}

    for layer_id in sorted(by_layer):
        layer_glyphs = by_layer[layer_id]
        params = layer_render_params.get(layer_id, render_params)
        hatched_el, contour_el = _build_layer_fragment(
            layer_glyphs, params, overrides=None, render_cache=render_cache
        )
        label = labels.get(layer_id, f"layer {layer_id}")
        anchor = last_element_for_layer[layer_id]
        pos = root.index(anchor) + 1

        if hatched_el is not None:
            _finalize_layer_group(hatched_el, used_ids, f"fonthatch-hatched-{layer_id}", f"{label} hatched", transform)
            root.insert(pos, hatched_el)
            pos += 1
        if contour_el is not None:
            _finalize_layer_group(contour_el, used_ids, f"fonthatch-contour-{layer_id}", f"{label} contour", transform)
            root.insert(pos, contour_el)

    return root


def compose_svg_string(
    input_svg_path: str,
    glyph_outlines: list[GlyphOutline],
    render_params: RenderParams,
    *,
    layer_render_params: dict[int, RenderParams] | None = None,
    render_cache: dict[tuple, tuple] | None = None,
    hide_original_text: bool = True,
) -> str:
    root = compose_svg(
        input_svg_path,
        glyph_outlines,
        render_params,
        layer_render_params=layer_render_params,
        render_cache=render_cache,
        hide_original_text=hide_original_text,
    )
    return etree.tostring(root, xml_declaration=True, encoding="utf-8", standalone=False).decode("utf-8")


def compose_svg_to_file(
    input_svg_path: str,
    output_svg_path: str,
    glyph_outlines: list[GlyphOutline],
    render_params: RenderParams,
    *,
    layer_render_params: dict[int, RenderParams] | None = None,
    render_cache: dict[tuple, tuple] | None = None,
    hide_original_text: bool = True,
) -> None:
    if Path(input_svg_path).resolve() == Path(output_svg_path).resolve():
        raise ValueError(
            f"Refusing to write output over the input file ({input_svg_path}) — "
            "choose a different output path."
        )
    svg_text = compose_svg_string(
        input_svg_path,
        glyph_outlines,
        render_params,
        layer_render_params=layer_render_params,
        render_cache=render_cache,
        hide_original_text=hide_original_text,
    )
    with open(output_svg_path, "w", encoding="utf-8") as f:
        f.write(svg_text)
