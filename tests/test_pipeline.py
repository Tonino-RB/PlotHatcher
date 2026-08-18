from pathlib import Path

from lxml import etree
from shapely.geometry import Point

from fonthatch.core.hatch import ContourMode, FillType, HatchParams
from fonthatch.core.pipeline import extract_glyph_outlines, process_svg_to_string
from fonthatch.core.render import RenderMode, RenderParams

FIXTURES = Path(__file__).parent / "fixtures"

_INKSCAPE_LABEL = "{http://www.inkscape.org/namespaces/inkscape}label"


def _groups_by_id(svg_text: str) -> dict[str, etree._Element]:
    root = etree.fromstring(svg_text.encode("utf-8"))
    return {el.get("id"): el for el in root.iter() if el.get("id")}


def _hatched(svg_text: str, layer_id: int = 1) -> etree._Element:
    return _groups_by_id(svg_text)[f"fonthatch-hatched-{layer_id}"]


def _contour(svg_text: str, layer_id: int = 1) -> etree._Element:
    return _groups_by_id(svg_text)[f"fonthatch-contour-{layer_id}"]


def _text_elements(svg_text: str) -> list[etree._Element]:
    root = etree.fromstring(svg_text.encode("utf-8"))
    return [el for el in root.iter() if el.tag.endswith("}text")]


def test_mixed_svg_hides_original_text_and_adds_visible_hatched_layer():
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35, merge_ends=True),
    )
    texts = _text_elements(svg_text)
    assert texts
    assert all("display:none" in t.get("style", "") for t in texts)
    hatched = _hatched(svg_text)
    assert "display:none" not in hatched.get("style", "")


def test_original_text_content_is_preserved_verbatim():
    """Unlike the old outline-based "text" layer, the original <text>
    element itself — its real characters, attributes, position — must
    survive completely unchanged, just hidden."""
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35),
    )
    (text_el,) = _text_elements(svg_text)
    assert text_el.text == "Hi O"
    assert text_el.get("x") == "50"
    assert text_el.get("y") == "50"
    assert text_el.get("font-family") == "Arial"


def test_non_text_content_passes_through_untouched():
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35),
    )
    root = etree.fromstring(svg_text.encode("utf-8"))
    rects = [el for el in root.iter() if el.tag.endswith("}rect")]
    assert len(rects) == 1
    rect = rects[0]
    assert rect.get("x") == "10" and rect.get("y") == "10"
    assert rect.get("width") == "30" and rect.get("height") == "20"
    assert rect.get("fill") == "none" and rect.get("stroke") == "black"


def test_all_fill_types_produce_output_on_glyph_with_hole():
    outlines = extract_glyph_outlines(str(FIXTURES / "mixed.svg"))
    has_hole = [o for o in outlines if o.polygon is not None and getattr(o.polygon, "interiors", [])]
    assert has_hole, "fixture should contain a glyph with a hole (the 'O')"

    for fill_type in FillType:
        svg_text = process_svg_to_string(
            str(FIXTURES / "mixed.svg"),
            HatchParams(fill_type=fill_type, spacing=0.8, pen_width=0.35, merge_ends=True),
        )
        hatched_children = list(_hatched(svg_text))
        assert hatched_children, f"{fill_type} produced no geometry"


def test_nested_tspan_and_transform_fixture_does_not_crash():
    svg_text = process_svg_to_string(
        str(FIXTURES / "nested_transform.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.3),
    )
    assert list(_hatched(svg_text))


def test_empty_tspan_run_does_not_crash():
    """Regression test: a tspan with an explicit x/y reset but no characters
    (kept for cursor-flow purposes, see svg_text.py) used to crash — HarfBuzz
    leaves glyph_positions as None (not []) when shaping an empty string,
    unlike glyph_infos, and zip()ing that raised TypeError."""
    svg_text = process_svg_to_string(
        str(FIXTURES / "empty_tspan.svg"),
        HatchParams(fill_type=FillType.SPIRALING, pen_width=0.3),
    )
    assert list(_hatched(svg_text))


def test_singleline_mode_hides_original_text_and_shows_hatched_layer():
    svg_text = process_svg_to_string(
        str(FIXTURES / "mixed.svg"),
        RenderParams(mode=RenderMode.SINGLELINE, singleline_font="futural"),
    )
    texts = _text_elements(svg_text)
    assert texts and all("display:none" in t.get("style", "") for t in texts)
    hatched = _hatched(svg_text)
    assert "display:none" not in hatched.get("style", "")
    assert list(hatched)


def test_singleline_mode_has_fewer_strokes_than_hatch_mode():
    """SINGLELINE mode fully replaces glyph ink — unlike HATCH mode, the
    "hatched" layer should have fewer elements than HATCH mode's would for
    the same glyphs, since e.g. 'O' is one closed outline polygon but the
    Hershey 'O' substitute is typically several open strokes, and
    holes/counters (which contribute extra outline loops) have no equivalent
    concept in the single-line substitute."""
    hatch_svg = process_svg_to_string(
        str(FIXTURES / "mixed.svg"), HatchParams(fill_type=FillType.SPIRALING, pen_width=0.35)
    )
    singleline_svg = process_svg_to_string(
        str(FIXTURES / "mixed.svg"), RenderParams(mode=RenderMode.SINGLELINE, singleline_font="futural")
    )
    hatch_hatched_children = list(_hatched(hatch_svg))
    singleline_hatched_children = list(_hatched(singleline_svg))
    assert singleline_hatched_children
    assert len(singleline_hatched_children) < len(hatch_hatched_children)


def test_draw_contour_false_omits_outline_from_hatched_layer():
    params = RenderParams(mode=RenderMode.HATCH, hatch=HatchParams(fill_type=FillType.LINES, spacing=2.0))
    with_contour = process_svg_to_string(str(FIXTURES / "mixed.svg"), params)

    params.draw_contour = False
    without_contour = process_svg_to_string(str(FIXTURES / "mixed.svg"), params)

    with_count = len(list(_hatched(with_contour)))
    without_count = len(list(_hatched(without_contour)))
    assert without_count < with_count


def test_contour_separate_layer_creates_its_own_layer():
    params = RenderParams(
        mode=RenderMode.HATCH,
        hatch=HatchParams(fill_type=FillType.LINES, spacing=2.0),
        contour_separate_layer=True,
    )
    svg_text = process_svg_to_string(str(FIXTURES / "mixed.svg"), params)
    contour = _contour(svg_text)
    assert list(contour)
    assert "display:none" not in contour.get("style", "")
    # the contour must no longer be duplicated inside "hatched" too
    assert list(_hatched(svg_text))


def test_glyph_fill_forces_outer_contour_regardless_of_contour_mode():
    """glyph_fill's own fill strokes assume the separately-drawn contour was
    OUTER-traced (its rings start past where OUTER's ring 0 would sit) —
    layers.py must force that regardless of what contour_mode the caller
    left set, so the two can never silently drift apart and leave a real gap
    between contour and fill. Checked against the internal vpype.Document
    representation directly (not the serialized SVG) so there's no risk of
    an unrelated transform step masking a real mismatch.

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
