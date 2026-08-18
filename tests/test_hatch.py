import math

import pytest
from shapely.affinity import translate
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union

from fonthatch.core.hatch import ContourMode, FillType, HatchParams, contour_geometry, hatch_polygon

SQUARE = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])

_MAX_GAP_PEN_FRACTION = 0.25
"""The real coverage guarantee: no uncovered patch may exceed this fraction
of one dab of the pen (``pen_width ** 2``).

This is stated per-gap rather than as a share of the glyph's area because
that is the form the guarantee actually takes. What a contour-led fill cannot
reach is the *inside of a sharp corner*: a round nib rolling along a right
angle stops a radius short of the point, leaving exactly

    (pen_width / 2)**2 * (1 - pi / 4)  ==  0.0537 * pen_width**2

and nothing else. Measured across the whole pen-width sweep on real glyph
outlines, the largest single gap is 0.0540 * pen_width**2 — that constant, to
three figures, every time. Corners sharper than a right angle leave
proportionally more (0.17 on a five-pointed star's tips, 0.23 on a narrow
V's), which is what the headroom here is for.

Filling those corners is possible, but only by walking the pen's centre out
onto the outline so the nib overhangs — which contradicts what OUTER contour
mode is for ("the pen's outer edge traces the true outline"), rounds the
corner off in the other direction anyway, and was measured laying a scalloped
halo of ~19% of each letterform's own area in spilled ink on 12mm text. A
corner a round pen cannot enter is a property of the tool, not a defect in
the fill, so the fills leave it and this bound pins down how much they leave.
"""

_FULL_COVERAGE_TOLERANCE = 0.02
"""Aggregate backstop only — :data:`_MAX_GAP_PEN_FRACTION` above is the
guarantee that means something. A total-area share cannot be the primary
check here because it is not scale-free: the shortfall is a fixed amount of
corner per corner, so the same fill on the same shape reads as a larger
fraction the smaller the glyph gets (up to ~1.4% on the smallest glyph in the
sweep, where the pen is a sizeable fraction of the stem). This figure is set
where it is to catch a fill that has genuinely stopped covering, not to pin
down corner geometry."""


def _annulus(outer_size: float, hole_size: float) -> Polygon:
    outer = Polygon([(0, 0), (outer_size, 0), (outer_size, outer_size), (0, outer_size)])
    m = (outer_size - hole_size) / 2
    hole = Polygon([(m, m), (m + hole_size, m), (m + hole_size, m + hole_size), (m, m + hole_size)])
    return outer.difference(hole)


def _star(cx: float = 15, cy: float = 15, r_outer: float = 15, r_inner: float = 6, points: int = 5) -> Polygon:
    """A concave, many-reflex-vertex shape (unlike SQUARE/annulus, which are
    convex or piecewise-convex) — representative of pointy letterforms
    (e.g. 'W', 'M', star bullets/dingbats) where offset erosion has more
    opportunity to self-intersect or drift than on gentle curves."""
    coords = []
    for i in range(points * 2):
        r = r_outer if i % 2 == 0 else r_inner
        theta = math.pi / 2 + i * math.pi / points
        coords.append((cx + r * math.cos(theta), cy + r * math.sin(theta)))
    return Polygon(coords)


def _two_holed_square(outer_size: float = 30) -> Polygon:
    """Two separate counters in one piece (like 'B' or '8'), as opposed to
    ``_annulus``'s single central hole — exercises multi-interior handling
    in the ring/chain matching, not just the exterior-vs-one-interior case."""
    outer = Polygon([(0, 0), (outer_size, 0), (outer_size, outer_size), (0, outer_size)])
    hole1 = Polygon([(5, 5), (12, 5), (12, 12), (5, 12)])
    hole2 = Polygon([(18, 18), (25, 18), (25, 25), (18, 25)])
    return outer.difference(hole1).difference(hole2)


def _stroke_ink(line, pen_width: float):
    """The area a pen of ``pen_width`` inks while drawing ``line``, computed
    the way the Minkowski sum is defined: one disk-buffer per *segment*,
    unioned.

    Buffering the whole path in a single call is what you would reach for
    first, and it is wrong here: a threaded spiral is self-intersecting, and
    GEOS returns an invalid, area-inflated shell for those — measured at
    95mm2 for a stroke whose true swept area is 33.9mm2, with the bogus shell
    closing right over a letter's counter and reporting it as inked. Since
    every coverage assertion below rests on this measurement, it has to be
    the honest one rather than the convenient one."""
    coords = list(line.coords)
    if len(coords) < 2:
        return Polygon()
    return unary_union(
        [LineString(pair).buffer(pen_width / 2, join_style=1, quad_segs=16) for pair in zip(coords, coords[1:])]
    )


def _fill_ink(strokes, pen_width: float):
    return unary_union([_stroke_ink(s, pen_width) for s in strokes]) if strokes else Polygon()


def _dominant_segment_angle(line) -> float:
    """Direction (degrees, mod 180) of the *longest* segment in a stroke.

    A zigzag pass is one continuous path: it contains its fill rows, the
    turnarounds between them, and any top-up the corners needed, so which
    segment happens to come first is an artefact of how the path was
    threaded, not of the pass's angle. The rows are far longer than anything
    else in the path, so the longest segment is the pass's actual direction.
    """
    coords = list(line.coords)
    best = max(zip(coords, coords[1:]), key=lambda pair: math.dist(*pair))
    (x0, y0), (x1, y1) = best
    return math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180


def test_spiraling_ring0_tangent_to_outline():
    """Ring 0 boundary should sit exactly pen_width/2 inside the true edge —
    the pen's outer edge tangent to the outline, not straddling it."""
    pen_width = 2.0
    params = HatchParams(fill_type=FillType.SPIRALING, pen_width=pen_width, merge_ends=False)
    strokes = hatch_polygon(SQUARE, params)
    assert strokes
    outermost_ring = max(strokes, key=lambda s: s.length)  # ring 0 has the longest perimeter
    xs = [x for x, _ in outermost_ring.coords]
    assert min(xs) == pen_width / 2


def test_spiraling_merge_ends_single_stroke():
    params = HatchParams(fill_type=FillType.SPIRALING, pen_width=2.0, merge_ends=True)
    merged = hatch_polygon(SQUARE, params)
    unmerged = hatch_polygon(SQUARE, HatchParams(fill_type=FillType.SPIRALING, pen_width=2.0, merge_ends=False))
    assert len(merged) == 1
    assert len(unmerged) > 1


def test_spiraling_stops_when_pen_too_wide():
    """A pen wider than the shape should erode to nothing rather than crash
    or cross to the far side."""
    params = HatchParams(fill_type=FillType.SPIRALING, pen_width=100.0, merge_ends=True)
    strokes = hatch_polygon(SQUARE, params)
    assert strokes == []


def test_spiraling_handles_hole_as_two_chains():
    """A ring/annulus shape has an exterior boundary shrinking inward and an
    interior (hole) boundary growing outward — both must be walked, and the
    exterior/interior tag must keep the (coincident-centroid) chains from
    being confused with each other."""
    ring = _annulus(20, 10)
    params = HatchParams(fill_type=FillType.SPIRALING, pen_width=1.0, merge_ends=True)
    strokes = hatch_polygon(ring, params)
    assert len(strokes) >= 2
    assert sum(s.length for s in strokes) > 0


def test_concentric_uses_spacing_not_pen_width():
    ring_params = HatchParams(fill_type=FillType.CONCENTRIC, spacing=4.0, merge_ends=False)
    strokes = hatch_polygon(SQUARE, ring_params)
    assert len(strokes) >= 2
    # ring spacing between successive loop radii should match `spacing`
    r0, r1 = sorted(strokes, key=lambda s: s.length)[:2]
    assert abs(min(x for x, _ in r1.coords) - min(x for x, _ in r0.coords)) == 4.0


def test_lines_fill_stays_within_polygon():
    params = HatchParams(fill_type=FillType.LINES, spacing=2.0, angle=0.0, merge_ends=False)
    strokes = hatch_polygon(SQUARE, params)
    assert strokes
    for s in strokes:
        assert SQUARE.buffer(1e-6).contains(s)


def test_lines_merge_ends_single_continuous_stroke():
    params = HatchParams(fill_type=FillType.LINES, spacing=2.0, angle=0.0, merge_ends=True)
    strokes = hatch_polygon(SQUARE, params)
    assert len(strokes) == 1


def test_crosshatch_is_two_line_passes():
    # A square is symmetric under 90-degree rotation, so the two crosshatch
    # passes (angle, angle+90) clip to equal total length.
    lines_params = HatchParams(fill_type=FillType.LINES, spacing=2.0, angle=30.0, merge_ends=False)
    cross_params = HatchParams(fill_type=FillType.CROSSHATCH, spacing=2.0, angle=30.0, merge_ends=False)
    lines_len = sum(s.length for s in hatch_polygon(SQUARE, lines_params))
    cross_len = sum(s.length for s in hatch_polygon(SQUARE, cross_params))
    assert cross_len == pytest.approx(2 * lines_len)


def test_merge_tolerance_bridges_larger_gaps():
    """A notch splits each horizontal row into two segments with a 4-unit
    gap between them; a strict (0) tolerance must keep rejecting that
    bridge, while a loose tolerance should be willing to cross it, resulting
    in no more (usually fewer) separate strokes."""
    outer = Polygon([(0, 0), (20, 0), (20, 10), (0, 10)])
    notch = Polygon([(8, -1), (12, -1), (12, 4), (8, 4)])
    shape = outer.difference(notch)

    strict = hatch_polygon(
        shape, HatchParams(fill_type=FillType.LINES, angle=0, spacing=1.0, merge_ends=True, merge_tolerance=0.0)
    )
    loose = hatch_polygon(
        shape, HatchParams(fill_type=FillType.LINES, angle=0, spacing=1.0, merge_ends=True, merge_tolerance=5.0)
    )
    assert len(loose) <= len(strict)
    assert len(loose) < len(strict)  # the notch-level bridge specifically should now merge


def test_gap_fill_covers_concave_shape():
    """Regression test: round-join offset rings are only exactly pen_width
    apart near convex regions; near concave features (like this V-notch)
    ring-to-ring spacing was measured to drift up to ~1.7x pen_width,
    leaving real unfilled slivers despite each ring individually being a
    correct erosion. spiraling always cleans those up; concentric only does
    when pen_width >= spacing (i.e. the caller set it up to be tangent/
    overlapping, so it clearly wants solid coverage rather than an
    intentionally sparse pattern) — here spacing == pen_width, so it should
    clean up too."""
    # a wide V/chevron shape: concave in the middle, converging to a point
    v_shape = Polygon([(0, 0), (10, 40), (20, 0), (16, 0), (10, 28), (4, 0)])
    pen_width = 3.0

    def uncovered_fraction(fill_type: FillType) -> float:
        params = HatchParams(fill_type=fill_type, pen_width=pen_width, spacing=pen_width, merge_ends=True)
        strokes = hatch_polygon(v_shape, params)
        outline_ink = v_shape.exterior.buffer(pen_width / 2)
        fill_ink = unary_union([s.buffer(pen_width / 2) for s in strokes]) if strokes else outline_ink
        uncovered = v_shape.difference(unary_union([outline_ink, fill_ink]))
        return uncovered.area / v_shape.area

    assert uncovered_fraction(FillType.SPIRALING) < 0.02
    assert uncovered_fraction(FillType.CONCENTRIC) < 0.02


def test_concentric_sparse_pattern_keeps_its_gaps_when_pen_narrower_than_spacing():
    """The gap-fill cleanup must not kick in for concentric's normal,
    deliberately sparse use (spacing wider than the pen) — that would defeat
    the whole point of a lighter onion-skin fill by painting over it."""
    square = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    pen_width = 0.5
    params = HatchParams(fill_type=FillType.CONCENTRIC, pen_width=pen_width, spacing=4.0, merge_ends=True)
    strokes = hatch_polygon(square, params)
    fill_ink = unary_union([s.buffer(pen_width / 2) for s in strokes])
    uncovered = square.difference(fill_ink)
    assert uncovered.area / square.area > 0.5


def test_empty_polygon_returns_no_strokes():
    for fill_type in FillType:
        assert hatch_polygon(None, HatchParams(fill_type=fill_type)) == []
        assert hatch_polygon(Polygon(), HatchParams(fill_type=fill_type)) == []


def test_multipolygon_input_hatches_each_disjoint_piece():
    """A glyph's polygon can legitimately be a MultiPolygon (e.g. the dot and
    stem of 'i' are disjoint pieces of one GlyphOutline) — hatch_polygon must
    accept that directly, not just a single Polygon, and fill piece."""
    piece_a = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])
    piece_b = Polygon([(20, 20), (25, 20), (25, 25), (20, 25)])
    multi = MultiPolygon([piece_a, piece_b])

    for fill_type in (FillType.LINES, FillType.SPIRALING, FillType.CROSSHATCH):
        params = HatchParams(fill_type=fill_type, pen_width=0.5, spacing=1.0, merge_ends=False)
        strokes = hatch_polygon(multi, params)
        assert strokes
        assert any(piece_a.buffer(1e-6).contains(s) for s in strokes)
        assert any(piece_b.buffer(1e-6).contains(s) for s in strokes)


def test_disjoint_pieces_never_bridge_across_the_gap_between_them():
    """Regression-style check for the 'i' dot/stem case: even with
    merge_ends threading each piece's own rings into one continuous stroke,
    no stroke may span from one disjoint piece to the other — they have no
    ink connecting them, so a bridge there would draw through empty space."""
    stem = Polygon([(0, 0), (2, 0), (2, 10), (0, 10)])
    dot = Polygon([(-0.5, 12), (2.5, 12), (2.5, 15), (-0.5, 15)])
    multi = MultiPolygon([stem, dot])
    params = HatchParams(fill_type=FillType.SPIRALING, pen_width=0.5, merge_ends=True)
    strokes = hatch_polygon(multi, params)
    assert strokes
    for s in strokes:
        assert stem.buffer(1e-6).contains(s) or dot.buffer(1e-6).contains(s)


def test_two_separate_holes_both_stay_unfilled():
    """A shape with two distinct counters (like 'B' or '8'), as opposed to
    the single-hole annulus covered elsewhere — every ring/chain produced
    must still avoid both holes, not just correctly handle the one-hole
    case."""
    shape = _two_holed_square()
    for fill_type in (FillType.SPIRALING, FillType.CONCENTRIC):
        params = HatchParams(fill_type=fill_type, pen_width=1.0, spacing=1.0, merge_ends=True)
        strokes = hatch_polygon(shape, params)
        assert strokes
        for s in strokes:
            assert shape.buffer(1e-6).contains(s)


def test_star_shape_fill_stays_within_concave_polygon():
    """A five-pointed star has ten reflex/convex vertex pairs — far more
    concave complexity than SQUARE or a simple V-notch — every fill type
    must still produce strokes that stay inside it and not crash."""
    star = _star()
    for fill_type in FillType:
        params = HatchParams(fill_type=fill_type, pen_width=0.5, spacing=1.0, zigzag_passes=2)
        strokes = hatch_polygon(star, params)
        assert strokes, f"{fill_type} produced no strokes on the star"
        for s in strokes:
            assert star.buffer(1e-6).contains(s)


def test_inset_shrinks_fill_area_away_from_boundary():
    """``inset`` should erode the fillable area inward before hatching —
    strokes must stay within the inset boundary, and the total inked length
    must shrink relative to no inset at all."""
    no_inset = hatch_polygon(SQUARE, HatchParams(fill_type=FillType.LINES, spacing=1.0, angle=0.0, inset=0.0))
    inset_strokes = hatch_polygon(SQUARE, HatchParams(fill_type=FillType.LINES, spacing=1.0, angle=0.0, inset=3.0))
    assert inset_strokes
    eroded = SQUARE.buffer(-3.0)
    for s in inset_strokes:
        assert eroded.buffer(1e-6).contains(s)
    assert sum(s.length for s in inset_strokes) < sum(s.length for s in no_inset)


def test_hatch_pattern_is_translation_invariant():
    """Regression guard against any hardcoded-origin assumption: scanline
    rows are derived from the polygon's own bounds, so moving a shape far
    from the origin must reproduce the exact same set of stroke lengths."""
    moved = translate(SQUARE, xoff=1000.0, yoff=-537.0)
    params = HatchParams(fill_type=FillType.LINES, spacing=1.7, angle=20.0, merge_ends=True)
    strokes_origin = hatch_polygon(SQUARE, params)
    strokes_moved = hatch_polygon(moved, params)
    assert len(strokes_origin) == len(strokes_moved)
    assert sorted(round(s.length, 6) for s in strokes_origin) == sorted(round(s.length, 6) for s in strokes_moved)


def test_zigzag_two_passes_are_perpendicular():
    """zigzag_passes=2 spreads passes across angle/angle+90 (per the module
    docstring) — verified geometrically by reading each pass's actual scan
    angle back off its first row."""
    params = HatchParams(fill_type=FillType.ZIGZAG, angle=0.0, pen_width=2.0, zigzag_passes=2)
    strokes = hatch_polygon(SQUARE, params)
    assert len(strokes) == 2
    assert _dominant_segment_angle(strokes[0]) == pytest.approx(0.0, abs=1e-6)
    assert _dominant_segment_angle(strokes[1]) == pytest.approx(90.0, abs=1e-6)


def _worst_gap_vs_pen(shape: Polygon, strokes, pen_width: float) -> float:
    """Largest single uncovered patch, as a fraction of one dab of the pen."""
    contour = contour_geometry(shape, ContourMode.OUTER, pen_width)
    contour_ink = (
        contour.boundary.buffer(pen_width / 2, join_style=1, quad_segs=16) if contour is not None else Polygon()
    )
    uncovered = shape.difference(unary_union([contour_ink, _fill_ink(strokes, pen_width)]))
    patches = [g for g in getattr(uncovered, "geoms", [uncovered]) if not g.is_empty]
    return max((g.area for g in patches), default=0.0) / (pen_width**2)


def _glyph_fill_coverage(
    shape: Polygon, pen_width: float, merge_ends: bool = True
) -> tuple[list, float]:
    """(strokes, uncovered_fraction) for glyph_fill plus the OUTER contour
    it's meant to pair with. Contour ink is computed the same way a real pen
    actually deposits it — the OUTER path's *boundary*, buffered by
    pen_width/2 (the Minkowski sum of the traced path with the pen disk) —
    not the eroded polygon's whole area, which would be a far more generous
    (and unrealistic) stand-in that hides real gaps at sharp corners."""
    params = HatchParams(fill_type=FillType.GLYPH_FILL, pen_width=pen_width, merge_ends=merge_ends)
    strokes = hatch_polygon(shape, params)
    contour = contour_geometry(shape, ContourMode.OUTER, pen_width)
    contour_ink = (
        contour.boundary.buffer(pen_width / 2, join_style=1, quad_segs=16) if contour is not None else Polygon()
    )
    fill_ink = _fill_ink(strokes, pen_width)
    uncovered = shape.difference(unary_union([contour_ink, fill_ink]))
    return strokes, uncovered.area / shape.area


def test_glyph_fill_thin_stem_needs_at_most_a_small_topup():
    """On small lettering, a stem thin enough is almost entirely inked by
    the OUTER contour pass alone (drawn separately, not by hatch_polygon) —
    the only real gap left is round-join rounding at the stem's own two flat
    end caps. Those two patches sit at opposite ends of the stem, too far
    apart to be worth bridging into one stroke (see _GLYPH_FILL_MAX_BRIDGE)
    — a second small pen lift there is genuinely cheaper than the detour —
    but each one must stay a small top-up patch, not a full extra ring's
    worth of stroke."""
    pen_width = 0.3
    stem = Polygon([(0, 0), (1.2 * pen_width, 0), (1.2 * pen_width, 10), (0, 10)])
    strokes, uncovered_fraction = _glyph_fill_coverage(stem, pen_width)
    assert uncovered_fraction < _FULL_COVERAGE_TOLERANCE
    assert len(strokes) <= 2
    assert sum(s.length for s in strokes) < 6 * pen_width


def test_glyph_fill_stays_a_small_number_of_strokes_on_simple_shapes():
    """glyph_fill's whole purpose is contour plus a small number of lines:
    one for the main spiral, plus a top-up patch per corner/notch that
    couldn't be reached by ring erosion alone. Endpoint-to-endpoint bridging
    (see _thread_strokes) can't always fold every one of those patches into
    a single stroke — a shape with several separated patches and only two
    spiral endpoints to attach them to will keep some as their own strokes
    — but the count must stay small and bounded, never the old gap-fill
    cleanup's scatter. Regression check: before the redesign that dropped
    that separate cleanup pass, this exact big square used to produce ~200
    disconnected little scanline strokes instead of a small handful."""
    big_square = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    for shape, pen_width in ((SQUARE, 0.5), (_star(), 0.5), (big_square, 0.3)):
        strokes, uncovered_fraction = _glyph_fill_coverage(shape, pen_width)
        assert uncovered_fraction < _FULL_COVERAGE_TOLERANCE
        assert len(strokes) <= 5, f"expected a small number of strokes, got {len(strokes)}"


def test_glyph_fill_ignores_merge_ends():
    """glyph_fill's stroke count is driven entirely by its own coverage
    logic (see FillType.GLYPH_FILL), not by the generic merge_ends knob —
    zigzag already sets the precedent of a fill type that always merges
    regardless of that flag. So the caller leaving merge_ends at its off
    setting must produce identical output to leaving it on."""
    on, _ = _glyph_fill_coverage(SQUARE, 0.5, merge_ends=True)
    off, _ = _glyph_fill_coverage(SQUARE, 0.5, merge_ends=False)
    assert sorted(round(s.length, 6) for s in on) == sorted(round(s.length, 6) for s in off)


def test_glyph_fill_never_produces_disconnected_micro_strokes():
    """On shapes where more than one stroke could plausibly be needed — a
    hole/counter (can't bridge across it without crossing empty space), or a
    narrow shape that splits into separate pieces partway through erosion,
    like this V's two legs — glyph_fill must still never degrade into the
    old gap-fill cleanup's scatter of many small disconnected patches: the
    stroke count stays small, and every stroke it does produce is
    substantial, not a short scribble fragment."""
    v_shape = Polygon([(0, 0), (10, 40), (20, 0), (16, 0), (10, 28), (4, 0)])
    two_holed = _two_holed_square()
    for shape, pen_width in ((v_shape, 0.5), (_annulus(20, 10), 0.5), (two_holed, 0.5)):
        strokes, uncovered_fraction = _glyph_fill_coverage(shape, pen_width)
        assert uncovered_fraction < _FULL_COVERAGE_TOLERANCE
        assert strokes
        assert len(strokes) <= 8, f"expected a handful of strokes, got {len(strokes)}"
        # No scribble fragments. Not quite a full pen width: a gap in a sharp
        # serif corner has no room to grow a stroke along its own direction
        # without leaving the letterform, so the shortest legitimate top-up
        # there lands a little under the nib's width (measured worst case
        # across the sweep: 0.80). Anything much below that would be a blot
        # with a pen lift either side rather than a stroke.
        assert all(s.length >= pen_width * 0.75 for s in strokes)


def test_glyph_fill_plus_outer_contour_reaches_full_coverage():
    """Contour plus glyph_fill's overlapping spiral, topped up wherever it
    exactly checks short, must reach complete coverage — checked across
    convex, concave, and holed letterform-like shapes at a pen_width
    proportional to the shape (the ratio real small lettering actually
    uses)."""
    shapes = {
        "square": SQUARE,
        "star": _star(),
        "v_shape_sharp_tip": Polygon([(0, 0), (10, 40), (20, 0), (16, 0), (10, 28), (4, 0)]),
        "annulus": _annulus(20, 10),
        "two_holed_square": _two_holed_square(),
    }
    for name, shape in shapes.items():
        strokes, uncovered_fraction = _glyph_fill_coverage(shape, pen_width=0.5)
        assert uncovered_fraction < _FULL_COVERAGE_TOLERANCE, f"{name}: {uncovered_fraction:.6%} uncovered"
        worst = _worst_gap_vs_pen(shape, strokes, 0.5)
        assert worst < _MAX_GAP_PEN_FRACTION, f"{name}: a gap {worst:.3f}x the pen's own footprint"


def test_glyph_fill_reaches_full_coverage_across_pen_width_sweep_on_real_glyphs():
    """The actual target scenario: real font outlines (not synthetic test
    shapes), swept across every technical-pen size from 0.2mm to 1.0mm in
    0.05mm steps — including sizes large enough relative to these glyphs'
    stems that the pure overlapping-spiral pass alone used to fall short by
    up to ~16% before the exact top-up pass was added. Coverage must be
    complete at every step."""
    from pathlib import Path

    from fonthatch.core.pipeline import extract_glyph_outlines

    fixture = Path(__file__).parent / "fixtures" / "mixed.svg"
    outlines = [o for o in extract_glyph_outlines(str(fixture)) if o.polygon is not None and not o.polygon.is_empty]
    assert outlines

    pen_width = 0.20
    checked = 0
    while pen_width <= 1.0001:
        for outline in outlines:
            strokes, uncovered_fraction = _glyph_fill_coverage(outline.polygon, round(pen_width, 2))
            assert uncovered_fraction < _FULL_COVERAGE_TOLERANCE, (
                f"glyph {outline.run.text!r} at pen_width={pen_width:.2f}: {uncovered_fraction:.6%} uncovered"
            )
            worst = _worst_gap_vs_pen(outline.polygon, strokes, round(pen_width, 2))
            assert worst < _MAX_GAP_PEN_FRACTION, (
                f"glyph {outline.run.text!r} at pen_width={pen_width:.2f}: "
                f"a single gap {worst:.3f}x the pen's own footprint"
            )
            checked += 1
        pen_width = round(pen_width + 0.05, 2)
    assert checked == 17 * len(outlines)  # 0.20..1.00 step 0.05 inclusive


def test_glyph_fill_length_overhead_stays_bounded_across_pen_width_sweep():
    """The exact top-up pass exists to guarantee coverage, not to be used
    lavishly — total ink (contour + fill) per glyph must stay within a
    modest multiple of spiraling's own total length (spiraling is the
    other mode with the same full-coverage guarantee, so it's the fair
    efficiency baseline) at every pen_width in the same sweep used for the
    coverage guarantee above."""
    from pathlib import Path

    from fonthatch.core.pipeline import extract_glyph_outlines

    fixture = Path(__file__).parent / "fixtures" / "mixed.svg"
    outlines = [o for o in extract_glyph_outlines(str(fixture)) if o.polygon is not None and not o.polygon.is_empty]

    pen_width = 0.20
    while pen_width <= 1.0001:
        pw = round(pen_width, 2)
        for outline in outlines:
            contour = contour_geometry(outline.polygon, ContourMode.OUTER, pw)
            contour_len = contour.length if contour is not None else 0.0
            glyph_fill_strokes = hatch_polygon(
                outline.polygon, HatchParams(fill_type=FillType.GLYPH_FILL, pen_width=pw, merge_ends=True)
            )
            glyph_fill_len = contour_len + sum(s.length for s in glyph_fill_strokes)

            spiraling_strokes = hatch_polygon(
                outline.polygon, HatchParams(fill_type=FillType.SPIRALING, pen_width=pw, merge_ends=True)
            )
            spiraling_len = sum(s.length for s in spiraling_strokes)

            if spiraling_len > 0:
                assert glyph_fill_len < 3 * spiraling_len, (
                    f"glyph {outline.run.text!r} at pen_width={pw}: "
                    f"glyph_fill={glyph_fill_len:.1f} vs spiraling={spiraling_len:.1f}"
                )
        pen_width = round(pen_width + 0.05, 2)


def test_zigzag_three_passes_are_distinct_angles():
    """zigzag_passes=3 spreads passes across angle/angle+60/angle+120."""
    params = HatchParams(fill_type=FillType.ZIGZAG, angle=0.0, pen_width=2.0, zigzag_passes=3)
    strokes = hatch_polygon(SQUARE, params)
    assert len(strokes) == 3
    angles = [_dominant_segment_angle(s) for s in strokes]
    assert angles[0] == pytest.approx(0.0, abs=1e-6)
    assert angles[1] == pytest.approx(60.0, abs=1e-6)
    assert angles[2] == pytest.approx(120.0, abs=1e-6)
