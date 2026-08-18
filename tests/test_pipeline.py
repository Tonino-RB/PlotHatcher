from pathlib import Path

from lxml import etree
from shapely.geometry import Point

from fonthatch.core.hatch import ContourMode, FillType, HatchParams
from fonthatch.core.pipeline import extract_glyph_outlines, process_svg_to_string
from fonthatch.core.render import RenderMode, RenderParams

FIXTURES = Path(__file__).parent / "fixtures"


def _layers(svg_text: str) -> dict[str, etree._Element]:
    root = etree.fromstring(svg_text.encode("utf-8"))
    out = {}
    for el in root.iter():
        label = el.get("{http://www.inkscape.org/namespaces/inkscape}label")
        if label:
            out[label] = el
    return out


def test_mixed_svg_has_hidden_text_and_visible_hatched_layers():
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35, merge_ends=True),
    )
    layers = _layers(svg_text)
    assert "text" in layers and "hatched" in layers
    assert "display:none" in layers["text"].get("style", "")
    assert "display:none" not in layers["hatched"].get("style", "")


def test_non_text_content_passes_through():
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35),
    )
    assert "10.0,10.0" in svg_text  # the original rect's corner, untouched


def test_all_fill_types_produce_output_on_glyph_with_hole():
    outlines = extract_glyph_outlines(str(FIXTURES / "mixed.svg"))
    has_hole = [o for o in outlines if o.polygon is not None and getattr(o.polygon, "interiors", [])]
    assert has_hole, "fixture should contain a glyph with a hole (the 'O')"

    for fill_type in FillType:
        svg_text = process_svg_to_string(
            str(FIXTURES / "mixed.svg"),
            HatchParams(fill_type=fill_type, spacing=0.8, pen_width=0.35, merge_ends=True),
        )
        layers = _layers(svg_text)
        hatched_children = list(layers["hatched"])
        assert hatched_children, f"{fill_type} produced no geometry"


def test_nested_tspan_and_transform_fixture_does_not_crash():
    svg_text = process_svg_to_string(
        str(FIXTURES / "nested_transform.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.3),
    )
    layers = _layers(svg_text)
    assert list(layers["hatched"])


def test_empty_tspan_run_does_not_crash():
    """Regression test: a tspan with an explicit x/y reset but no characters
    (kept for cursor-flow purposes, see svg_text.py) used to crash — HarfBuzz
    leaves glyph_positions as None (not []) when shaping an empty string,
    unlike glyph_infos, and zip()ing that raised TypeError."""
    svg_text = process_svg_to_string(
        str(FIXTURES / "empty_tspan.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.3),
    )
    layers = _layers(svg_text)
    assert list(layers["hatched"])


def test_singleline_mode_produces_hidden_text_and_visible_hatched_layers():
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        RenderParams(mode=RenderMode.SINGLELINE, singleline_font="futural"),
    )
    layers = _layers(svg_text)
    assert "text" in layers and "hatched" in layers
    assert "display:none" in layers["text"].get("style", "")
    assert "display:none" not in layers["hatched"].get("style", "")
    assert list(layers["hatched"])


def test_singleline_mode_has_no_outline_only_substitute_strokes():
    """SINGLELINE mode fully replaces glyph ink — unlike HATCH mode, the
    "hatched" layer should have fewer elements than the "text" (outline-only)
    layer would for the same glyphs, since e.g. 'O' is one closed outline
    polygon but the Hershey 'O' substitute is typically several open strokes,
    and holes/counters (which contribute extra outline loops) have no
    equivalent concept in the single-line substitute."""
    hatch_svg = process_svg_to_string(
        str(FIXTURES / "mixed.svg"), HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35)
    )
    singleline_svg = process_svg_to_string(
        str(FIXTURES / "mixed.svg"), RenderParams(mode=RenderMode.SINGLELINE, singleline_font="futural")
    )
    hatch_hatched_children = list(_layers(hatch_svg)["hatched"])
    singleline_hatched_children = list(_layers(singleline_svg)["hatched"])
    assert singleline_hatched_children
    # hatch mode adds many extra fill strokes on top of the outline; singleline mode does not
    assert len(singleline_hatched_children) < len(hatch_hatched_children)


def test_draw_contour_false_omits_outline_from_hatched_layer():
    params = RenderParams(mode=RenderMode.HATCH, hatch=HatchParams(fill_type=FillType.LINES, spacing=2.0))
    with_contour = process_svg_to_string(str(FIXTURES / "mixed.svg"), params)

    params.draw_contour = False
    without_contour = process_svg_to_string(str(FIXTURES / "mixed.svg"), params)

    with_count = len(list(_layers(with_contour)["hatched"]))
    without_count = len(list(_layers(without_contour)["hatched"]))
    assert without_count < with_count


def test_contour_separate_layer_creates_third_layer():
    params = RenderParams(
        mode=RenderMode.HATCH,
        hatch=HatchParams(fill_type=FillType.LINES, spacing=2.0),
        contour_separate_layer=True,
    )
    svg_text = process_svg_to_string(str(FIXTURES / "mixed.svg"), params)
    layers = _layers(svg_text)
    assert "contour" in layers
    assert list(layers["contour"])
    assert "display:none" not in layers["contour"].get("style", "")
    # the contour must no longer be duplicated inside "hatched" too
    assert list(layers["hatched"])


def test_glyph_fill_forces_outer_contour_regardless_of_contour_mode():
    """glyph_fill's own fill strokes assume the separately-drawn contour was
    OUTER-traced (its rings start past where OUTER's ring 0 would sit) —
    layers.py must force that regardless of what contour_mode the caller
    left set, so the two can never silently drift apart and leave a real gap
    between contour and fill. Checked against the internal vpype.Document
    representation directly (not the serialized SVG) so there's no risk of
    an unrelated transform/quantization step masking a real mismatch.

    Checked on the *contour* layer specifically (contour_separate_layer=True
    keeps it out of "hatched"), not the combined "hatched" layer — glyph_fill's
    own exact top-up pass (see FillType.GLYPH_FILL) can legitimately place a
    "hatched"-layer point right up against the true boundary when a real gap
    reaches it, so "hatched" content touching the true edge no longer proves
    anything either way; only the contour trace itself unambiguously does."""
    from fonthatch.core.layers import build_document

    pen_width = 0.35
    outlines = extract_glyph_outlines(str(FIXTURES / "mixed.svg"))
    glyph = next(o for o in outlines if o.polygon is not None and not o.polygon.is_empty)

    params = RenderParams(
        mode=RenderMode.HATCH,
        hatch=HatchParams(fill_type=FillType.GLYPH_FILL, pen_width=pen_width),
        contour_mode=ContourMode.CENTERLINE,  # deliberately left at the default, non-paired mode
        contour_separate_layer=True,
    )
    # Render only this one glyph, so every resulting point can be checked
    # against its polygon specifically (other glyphs' contour points would
    # legitimately fall outside it).
    doc, _text_id, _hatched_id, contour_id = build_document(str(FIXTURES / "mixed.svg"), [glyph], params)
    contour_points = [(pt.real, pt.imag) for line in doc.layers[contour_id].lines for pt in line]
    assert contour_points

    # CENTERLINE traces the true boundary exactly, so points would land on
    # it; OUTER never touches it — every contour-layer point must stay
    # strictly inside the true glyph polygon, which only holds if OUTER (not
    # the requested CENTERLINE) was actually used to draw the contour.
    inside = glyph.polygon.buffer(-1e-6)
    assert all(inside.contains(Point(x, y)) for x, y in contour_points)


def test_contour_mode_outer_stays_inside_true_outline():
    """OUTER mode's contour should be strictly inside the true glyph
    boundary (offset inward by pen_width/2), unlike CENTERLINE which
    straddles it — verified against the raw polygon, not the rendered SVG."""
    outlines = extract_glyph_outlines(str(FIXTURES / "mixed.svg"))
    glyph = next(o for o in outlines if o.polygon is not None and not o.polygon.is_empty)

    from fonthatch.core.hatch import contour_geometry

    pen_width = 2.0
    outer = contour_geometry(glyph.polygon, ContourMode.OUTER, pen_width)
    assert outer is not None
    assert glyph.polygon.buffer(-1e-6).contains(outer)
    assert not glyph.polygon.equals(outer)
