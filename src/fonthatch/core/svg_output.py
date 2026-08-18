"""Write a vpype.Document to SVG, with support for hiding specific layers.

vpype's own SVG writer always emits ``style="display:inline"`` on every
layer group — there is no hidden-layer option in its API — so hiding the
"text" layer requires a small lxml post-process pass after vpype writes the
document.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

import vpype
from lxml import etree

_INKSCAPE_LABEL = "{http://www.inkscape.org/namespaces/inkscape}label"


def render_svg(document: vpype.Document, hidden_layer_ids: list[int] | None = None, **write_svg_kwargs) -> str:
    buf = io.StringIO()
    vpype.write_svg(buf, document, **write_svg_kwargs)
    svg_text = buf.getvalue()
    if not hidden_layer_ids:
        return svg_text

    root = etree.fromstring(svg_text.encode("utf-8"))
    hidden_ids = {f"layer{layer_id}" for layer_id in hidden_layer_ids}
    for el in root.iter():
        if el.get("id") in hidden_ids:
            set_display_none(el)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8", standalone=False).decode("utf-8")


def set_display_none(el: etree._Element) -> None:
    style = el.get("style", "")
    props = dict(
        (part.split(":", 1)[0].strip(), part.split(":", 1)[1].strip()) for part in style.split(";") if ":" in part
    )
    props["display"] = "none"
    el.set("style", ";".join(f"{k}:{v}" for k, v in props.items()))


def write_svg(
    document: vpype.Document,
    output_path: str,
    hidden_layer_ids: list[int] | None = None,
    **write_svg_kwargs,
) -> None:
    svg_text = render_svg(document, hidden_layer_ids, **write_svg_kwargs)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(svg_text)


def hide_layers_in_file(path: str, layer_names: Iterable[str]) -> None:
    """Post-process an already-written SVG file, setting display:none on any
    Inkscape layer group whose label matches one of ``layer_names``.

    Used by the vsketch GUI's ``post_finalize`` hook: vsketch's own native
    save flow writes through vpype's writer directly (same
    always-display:inline limitation as ``vpype write``), so the "text"
    layer needs to be hidden after the fact rather than during writing.
    """
    names = set(layer_names)
    tree = etree.parse(path)
    root = tree.getroot()
    changed = False
    for el in root.iter():
        if etree.QName(el).localname == "g" and el.get(_INKSCAPE_LABEL) in names:
            set_display_none(el)
            changed = True
    if changed:
        tree.write(path, xml_declaration=True, encoding="utf-8", standalone=False)
