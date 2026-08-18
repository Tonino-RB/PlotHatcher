"""Extract <text>/<tspan> runs from an SVG, fully resolved to document space.

Walks the raw lxml tree directly for structure, ordering, transform
accumulation, and CSS-style/attribute font-property inheritance, rather than
relying on ``svgelements``' flat element iterator for any of that.

That switch is load-bearing, not a style choice: ``svgelements`` (verified
against the installed version) yields a *nested* tspan's ``Text`` object
*before* its containing ``<text>``/``<tspan>``'s own direct text — e.g. for
``<text>Ab<tspan>C</tspan></text>`` it yields "C" first, then "Ab" — the
reverse of document order. The original implementation grouped runs by
trusting that iteration order, which silently produced wrong layouts (a
run's inherited/explicit x/y got attributed to the wrong element) for any
SVG with nested tspans — common in real Inkscape/Illustrator exports, e.g.
Inkscape's own text-flow-into-shape feature wraps the actual text in two
levels of tspan. ``svgelements`` is still used as a small utility (parsing
individual ``transform`` attribute strings into matrices via ``se.Matrix``),
just no longer for structure or ordering.

Only horizontal, left-to-right, non-textPath layout is supported: multi-value
x/y lists (per-character positioning), bidi and vertical text are out of
scope for v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import svgelements as se
import vpype
from lxml import etree

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"^\s*(-?[0-9.eE+-]+)")
_LID_RE = re.compile(r"\d+")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_INKSCAPE_LABEL = "{http://www.inkscape.org/namespaces/inkscape}label"

_INHERITED_PROPS = ("font-family", "font-size", "font-weight", "font-style", "text-anchor", "fill")
_DEFAULT_PROPS = {
    "font-family": "sans-serif",
    "font-size": "16",
    "font-weight": "normal",
    "font-style": "normal",
    "text-anchor": "start",
    "fill": None,
}


@dataclass
class TextRun:
    text: str
    x: float | None
    y: float | None
    dx: float
    dy: float
    font_family: str
    font_size: float
    font_weight: str
    font_style: str
    text_anchor: str
    fill: str | None
    transform: se.Matrix
    """Maps this run's local (pre-layout) coordinate space to document px space."""
    block_index: int
    """1-based index of this run's containing top-level <text> element, in
    document order — lets downstream consumers group glyphs back by which
    original <text> element they came from, without re-parsing the SVG."""
    layer_index: int
    """Which of the SVG's own top-level layers this run's <text> lives in,
    numbered exactly the way ``vpype.io.read_multilayer_svg`` numbers them
    (see ``_toplevel_layer_ids``) — so it matches the same layer the vpype
    viewer's native "Layer" toggle already shows for the rest of the
    document, letting the GUI's per-layer override feature group text back
    by *that* instead of by individual <text> element."""


@dataclass
class TextBlock:
    """All runs belonging to a single top-level ``<text>`` element, in order."""

    runs: list[TextRun]

    @property
    def transform(self) -> se.Matrix:
        return self.runs[0].transform


def _local_name(tag) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_num(value: str | None) -> float | None:
    if value is None:
        return None
    m = _NUM_RE.match(value)
    return float(m.group(1)) if m else None


def _length_px(value: str | None, default: float) -> float:
    if not value:
        return default
    value = value.strip()
    if value.endswith("%"):
        # A percentage isn't an absolute length here (no containing block to
        # resolve it against) — treat it the same as absent, i.e. fall back
        # to the caller's default. Without this, the regex fallback below
        # would parse e.g. "100%" as the raw number 100, silently treating
        # a standalone-SVG-standard "intrinsic size == viewBox size" marker
        # as a tiny 100 user-unit width and shrinking the whole document
        # (text included) by whatever factor viewBox / 100 works out to.
        return default
    try:
        return vpype.convert_length(value)
    except ValueError:
        return _parse_num(value) or default


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text)


def _parse_style(style: str) -> dict[str, str]:
    props = {}
    for decl in style.split(";"):
        if ":" not in decl:
            continue
        k, v = decl.split(":", 1)
        props[k.strip().lower()] = v.strip()
    return props


def _own_props(el) -> dict[str, str]:
    """Only this element's own attributes/style — never inherited — so the
    caller can tell what's genuinely set here vs. cascaded from an ancestor."""
    props = {}
    for name in _INHERITED_PROPS:
        v = el.get(name)
        if v is not None:
            props[name] = v
    style = el.get("style")
    if style:
        props.update(_parse_style(style))
    return props


def _parse_transform(transform_str: str | None) -> se.Matrix:
    if not transform_str:
        return se.Matrix()
    try:
        return se.Matrix(transform_str)
    except ValueError:
        return se.Matrix()


def root_viewport_matrix(root) -> tuple[se.Matrix, float, float]:
    """Maps viewBox-space coordinates to document px space (96dpi CSS px,
    matching vpype's own internal convention), folding in any physical
    width/height <-> viewBox scale mismatch. Also returns that document-px
    (width, height) — the same page size vpype's own reader/writer would use
    for this file — since callers that need to place fresh geometry back
    into viewBox space (see compose.py) need both the matrix and the size it
    was derived from."""
    viewbox = root.get("viewBox")
    if not viewbox:
        return se.Matrix(), 0.0, 0.0
    parts = re.split(r"[ ,]+", viewbox.strip())
    if len(parts) != 4:
        return se.Matrix(), 0.0, 0.0
    try:
        minx, miny, vb_w, vb_h = (float(p) for p in parts)
    except ValueError:
        return se.Matrix(), 0.0, 0.0
    if vb_w == 0 or vb_h == 0:
        return se.Matrix(), 0.0, 0.0

    width = _length_px(root.get("width"), vb_w)
    height = _length_px(root.get("height"), vb_h)
    scale_x = width / vb_w
    scale_y = height / vb_h
    matrix = se.Matrix(scale_x, 0, 0, scale_y, -scale_x * minx, -scale_y * miny)
    return matrix, width, height


def _root_viewport_matrix(root) -> se.Matrix:
    return root_viewport_matrix(root)[0]


def _extract_digit_group(label: str | None) -> str | None:
    if not label:
        return None
    m = _LID_RE.search(label)
    return m.group() if m else None


def _toplevel_layer_ids(root) -> list[int]:
    """Index i (matching ``enumerate(root)``, i.e. position among the SVG
    root's direct children) -> vpype-style layer id. Keyed by position
    rather than ``id(element)``: lxml doesn't guarantee a stable Python
    object identity for the same underlying node across separate
    iterations, so ``id()`` of a child fetched in one pass over ``root``
    can't be relied on to match ``id()`` of "the same" child fetched in a
    later pass.

    Mirrors ``vpype.io.read_multilayer_svg``'s own grouping exactly (a
    private implementation detail there, so reimplemented rather than
    imported): each top-level ``<g>`` is a layer, matched to an id via the
    first digit group in its ``inkscape:label``, else its ``id`` attribute,
    else its appearing order among sibling groups (1-based); every
    non-group top-level element (bare ``<text>``, ``<rect>``, ...) falls
    into layer 1, same as vpype's "non-group elements go to layer 1" rule."""
    ids: list[int] = []
    group_index = 0
    for child in root:
        if _local_name(child.tag) != "g":
            ids.append(1)
            continue
        lid_str = _extract_digit_group(child.get(_INKSCAPE_LABEL)) or _extract_digit_group(child.get("id"))
        if lid_str:
            lid = int(lid_str)
            if lid == 0:
                lid = 1
        else:
            lid = group_index + 1
        group_index += 1
        ids.append(lid)
    return ids


toplevel_layer_ids = _toplevel_layer_ids
"""Public alias — used outside this module (see compose.py) to graft new
content onto the same top-level layer a given TextRun.layer_index came
from."""


def toplevel_layer_labels(root) -> dict[int, str]:
    """layer id -> the human-readable label ``compose.py`` should derive its
    new "<label> hatched"/"<label> contour" layer names from: an Inkscape
    ``inkscape:label``, else a plain ``id``, for whichever top-level ``<g>``
    first introduced that layer id (see ``_toplevel_layer_ids``). A layer id
    with no labelled group backing it (e.g. the "bare elements go to layer
    1" catch-all) is simply absent — callers fall back to a generic name."""
    labels: dict[int, str] = {}
    for child, lid in zip(root, _toplevel_layer_ids(root)):
        if lid in labels or _local_name(child.tag) != "g":
            continue
        label = child.get(_INKSCAPE_LABEL) or child.get("id")
        if label:
            labels[lid] = label
    return labels


def _collect_runs(
    el,
    inherited: dict,
    transform: se.Matrix,
    preserve: bool,
    runs: list[TextRun],
    pending: dict,
    block_index: int,
    layer_index: int,
) -> None:
    """``pending`` carries a not-yet-applied x/y reset and accumulated dx/dy
    down through the tree, shared (mutated in place) across the whole
    recursive walk of one <text>. A tspan with x/y but no direct text of its
    own — e.g. Inkscape's shape-inside wrapper, ``<tspan x=".." y=".."><tspan
    style="...">actual text</tspan></tspan>`` — must still have that
    position land on the first real character found anywhere in its
    subtree, not just on its own (possibly absent) direct text."""
    resolved = {**inherited, **_own_props(el)}
    node_transform = _parse_transform(el.get("transform")) * transform

    space = el.get(_XML_SPACE)
    node_preserve = preserve if space is None else (space == "preserve")

    own_x = _parse_num(el.get("x"))
    own_y = _parse_num(el.get("y"))
    own_dx = _parse_num(el.get("dx"))
    own_dy = _parse_num(el.get("dy"))
    if own_x is not None:
        pending["x"] = own_x
    if own_y is not None:
        pending["y"] = own_y
    if own_dx is not None:
        pending["dx"] = pending["dx"] + own_dx
    if own_dy is not None:
        pending["dy"] = pending["dy"] + own_dy

    def make_run(text: str) -> None:
        clean = text if node_preserve else _collapse_ws(text)
        x, y = pending["x"], pending["y"]
        if clean == "" and x is None and y is None:
            return  # contributes nothing: neither glyphs nor a cursor reset
        runs.append(
            TextRun(
                text=clean,
                x=x,
                y=y,
                dx=pending["dx"],
                dy=pending["dy"],
                font_family=resolved.get("font-family") or "sans-serif",
                font_size=_length_px(resolved.get("font-size"), 16.0),
                font_weight=resolved.get("font-weight") or "normal",
                font_style=resolved.get("font-style") or "normal",
                text_anchor=resolved.get("text-anchor") or "start",
                fill=resolved.get("fill"),
                transform=node_transform,
                block_index=block_index,
                layer_index=layer_index,
            )
        )
        pending["x"] = None
        pending["y"] = None
        pending["dx"] = 0.0
        pending["dy"] = 0.0

    if el.text:
        make_run(el.text)

    for child in el:
        if _local_name(child.tag) == "tspan":
            _collect_runs(child, resolved, node_transform, node_preserve, runs, pending, block_index, layer_index)
        if child.tail:
            # Text after a child's closing tag belongs to *this* element's
            # flow — no x/y of its own, it just continues the cursor (any
            # dx/dy still pending from an empty child, however, does apply).
            make_run(child.tail)


def _walk(el, transform: se.Matrix, blocks: list[TextBlock], layer_index: int) -> None:
    if _local_name(el.tag) == "text":
        # _collect_runs applies el's own transform itself (it's the shared
        # entry point for the recursive text/tspan walk) — pass the
        # ancestor transform as-is, not pre-multiplied by el's own here too.
        runs: list[TextRun] = []
        preserve = el.get(_XML_SPACE) == "preserve"
        pending = {"x": None, "y": None, "dx": 0.0, "dy": 0.0}
        # 1-based, and only counting blocks actually kept below (a <text>
        # with no real text is discarded, not numbered).
        _collect_runs(el, _DEFAULT_PROPS, transform, preserve, runs, pending, len(blocks) + 1, layer_index)
        if runs:
            runs[0].text = runs[0].text.lstrip(" ") if not preserve else runs[0].text
            runs[-1].text = runs[-1].text.rstrip(" ") if not preserve else runs[-1].text
        runs = [r for r in runs if r.text or r.x is not None or r.y is not None]
        if any(r.text for r in runs):
            blocks.append(TextBlock(runs=runs))
        return  # a <text> can't validly contain a nested <text>

    node_transform = _parse_transform(el.get("transform")) * transform
    for child in el:
        _walk(child, node_transform, blocks, layer_index)


def extract_text_blocks(svg_path: str) -> list[TextBlock]:
    """Parse ``svg_path`` and return all text blocks in document order."""
    root = etree.parse(svg_path).getroot()
    viewport_matrix, _width, _height = root_viewport_matrix(root)
    layer_ids = _toplevel_layer_ids(root)
    blocks: list[TextBlock] = []
    for i, child in enumerate(root):
        _walk(child, viewport_matrix, blocks, layer_ids[i])
    return blocks
