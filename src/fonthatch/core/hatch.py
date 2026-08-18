"""Pen-plotter hatch/fill engine, operating on shapely polygons.

Everything here is shaped by one constraint: the output is drawn by a pen,
not rasterised. Three things cost time and quality on a plotter — pen lifts
(each one is a travel move plus a dot of bleed where the nib lands), total
drawn length, and ink that lands outside the letterform. So each fill is
judged on strokes, length, coverage and spill, in that order, and the shared
machinery below (threading, top-up, ink measurement) exists to keep all four
in hand rather than trading one for another.

Six fill types:

- ``spiraling``: successive erosion by the pen radius — standard
  CNC-pocketing-style offset toolpath — threaded into one continuous spiral.
  Ring 0 is offset inward by ``pen_width / 2`` so the pen's *outer* edge
  (not its centerline) is tangent to the true outline; each further ring is
  offset by another full ``pen_width`` so adjacent strokes are tangent.
  Rings are traced *in full* and joined to the next at the true nearest
  point on it (found by projecting, not by snapping to a vertex — see
  :func:`_roll_to`), which costs one short radial jog per revolution but
  makes coverage exactly the rings' own coverage. (Morphing consecutive
  rings into a seamless spiral was tried instead — see the note on
  ``_spiral_from_chain`` — and loses coverage where the ring-to-ring
  correspondence stretches.)
- ``concentric``: the glyph's outline repeated inward as many times as it
  fits, spaced by the generic ``spacing`` param — an onion-skin pattern
  rather than a solid fill, so its gaps are the point and are left alone.
  Ring 0 sits one ``spacing`` inside the OUTER contour, which already draws
  the outermost loop, so the motif reads as evenly spaced from the edge
  instead of doubling the contour.
- ``lines``: classic parallel scanline hatch at a configurable angle.
- ``crosshatch``: ``lines`` plus a second pass at ``angle + 90``.
- ``zigzag``: ``lines`` made continuous — one long back-and-forth stroke
  rather than a stack of separate ones. Spans are walked as a graph so a
  single pen-down path keeps running through branching letterforms (the two
  stems of a 'u', the arms of a 't') instead of breaking at every row that
  splits in two.
- ``glyph_fill``: contour-led. The ``OUTER`` contour pass already lays a full
  pen width just inside the true edge, so the fill starts one pen further in
  and adds only what the contour could not reach. On small lettering, where
  a stem is no wider than the contour already inks, that is *nothing* — the
  mode's whole point. Pairs with :data:`ContourMode.OUTER`, which
  :func:`~fonthatch.core.layers.add_text_hatched_layers` forces
  automatically whenever this fill type is selected.

``spiraling``, ``zigzag`` and ``glyph_fill`` all promise complete coverage
and all reach it the same way: lay the primary pattern, measure what it
genuinely missed (see :func:`_ink_of`), cover the leftovers with
:func:`_topup`, then thread the result into as few strokes as possible with
:func:`_thread`. That promise is what ``HatchParams.guarantee_coverage``
(on by default) turns off: skip :func:`_topup` and a wide ``fill_spacing``
plots as the open, faster pattern it was set to instead of getting quietly
filled back in solid. Leaving ``fill_spacing`` at its own default too picks
one full ``pen_width`` (tangent, solid) when ``guarantee_coverage`` is on,
or ``pen_width * _OPEN_SPACING_FACTOR`` when it's off — so the flag has
something to visibly open up on its own, without also needing
``fill_spacing`` set by hand.

``merge_ends`` threads consecutive rings/rows into one continuous stroke
instead of lifting the pen between each. Every bridge is checked to stay
inside the glyph's own polygon first; one that would cut across a counter,
or across the gap between disconnected pieces (the dot and stem of an 'i'),
ends that stroke and starts a new one rather than drawing through empty
space. Strokes that can't be merged are still *ordered* by proximity, so the
pen-up travel between them stays as short as we can cheaply make it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np
import shapely
from shapely.affinity import rotate as shapely_rotate
from shapely.geometry import LinearRing, LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry

_JOIN_ROUND = 1
_BUFFER_RESOLUTION = 16
_MAX_RINGS = 2000

_INK_CHUNK_POINTS = 8
"""How many points of a stroke go into one buffer() call when measuring the
ink it deposits (see :func:`_stroke_ink`). GEOS's buffer() mis-handles a
self-intersecting input ring: on a real spiral stroke it was measured
returning an invalid polygon of 95mm² for a path whose true swept area is
33mm², and — because the bogus shell closed over the letter's counter — it
reported that counter as inked when no stroke went anywhere near it. Any
chunk short enough to stay simple avoids that; 8 points is small enough to
be reliably simple on the densest ring geometry here while keeping the
number of union operands (and so the cost) low."""

_TOPUP_MIN_AREA_FRACTION = 0.02
"""Floor, as a fraction of ``pen_width**2``, below which a leftover patch is
not worth a stroke: a gap that much smaller than the nib is narrower than
the ink actually spreads when it lands. Also filters the float noise that
boolean geometry leaves on genuinely covered shapes (observed at ~1e-7)."""

_TOPUP_SPACING = 0.8
"""Row spacing for the top-up pass, as a fraction of ``pen_width`` — tighter
than the tangent spacing the main patterns use. A leftover patch is an
awkward shape with sloping edges, and rows laid down exactly tangent leave a
staircase of small diamonds along such an edge (measured as 20 identical
0.25mm2 gaps marching down the diagonal between two counters — the whole of
one shape's remaining shortfall). Overlapping the rows slightly costs a few
millimetres of ink and removes the artefact outright; this is the "a bit of
overlap rather than a gap" tolerance applied to the one pass whose target
geometry is not under our control."""

_TOPUP_DILATE = 0.25
"""How far (as a fraction of ``pen_width``) a leftover patch is grown before
being filled, so the top-up stroke starts inside ink that is already there
rather than butting up against it exactly — a little overlap instead of a
hairline gap."""

_TOPUP_DOMAIN_STEPS = (0.5, 0.35, 0.2, 0.0)
"""Erosion depths, as fractions of ``pen_width``, tried in turn to find a
region for the pen *centre* to travel in while topping up a gap. The first
(pen_width/2) keeps every drop of ink inside the letterform. But a feature
thinner than the pen has no such region at all, and holding to it would
leave that feature simply blank — so the constraint is relaxed step by step
down to the raw outline, inking the feature along its centre and letting the
nib overhang. Overhang is the only way to mark a stem narrower than the nib,
and it is what keeps small text legible rather than hollow."""

_OPEN_SPACING_FACTOR = 2.0
"""What ``HatchParams.fill_spacing`` defaults to, as a multiple of
``pen_width``, when ``guarantee_coverage`` is off and the caller hasn't set
``fill_spacing`` explicitly. At the *other* default — one full ``pen_width``,
used when ``guarantee_coverage`` is on — the primary pattern is already
~solid, so turning ``guarantee_coverage`` off would otherwise change nothing
to look at: there'd be nothing left for the skipped top-up pass to have been
adding back. This gives "off" a spacing it can actually open up, without
changing what "on" draws (which still resolves to plain ``pen_width``) or
requiring ``fill_spacing`` to be set by hand just to see the checkbox do
anything."""

_SPIRAL_JOG_FACTOR = 1.8
"""How far, as a multiple of the ring spacing, the hop from one ring to the
next may be before it is treated as a break rather than a jog. A genuine
ring-to-ring transition is one spacing long; anything much longer means the
chain has split and the next ring is somewhere else entirely."""

_GLYPH_FILL_OVERLAP = 0.85
"""glyph_fill's ring spacing, as a fraction of ``pen_width`` — deliberately
under the tangent 1.0 the other modes use, so successive rings overlap
slightly.

Tangent rings are only genuinely tangent along straight edges. Round the
corner of a ring the separation grows to ``spacing / sin(theta/2)`` — a
factor of 1.41 at a right angle — which opens a real notch at every corner
that no amount of topping up cleans up neatly: the top-up stroke's own round
cap leaves a smaller crescent just past its end, and the next pass a smaller
one again. Overlapping the rings stops the notch forming at all, which is
much cheaper than chasing it afterwards. Tuned as the *loosest* (least ink)
value that still holds full coverage across the pen-width sweep in
test_hatch.py, rather than picked for headroom."""

_GLYPH_FILL_MAX_BRIDGE = 15.0
"""How far (as a multiple of ``pen_width``) glyph_fill's strokes may bridge
to each other rather than taking a pen lift. Too low and several patches on
one letter needlessly scatter; too high and a bridge stops meaning "nearby"
— it merely passes the stays-inside-the-ink check while retracing most of a
long stem to get there, where a second pen lift is genuinely cheaper."""

_CONCENTRIC_MIN_RING_FACTOR = 1.5
"""Rings shorter than this multiple of the spacing are dropped: the last
erosion step before a region vanishes leaves degenerate slivers, which plot
as ink blots rather than as part of the pattern."""


class FillType(str, Enum):
    SPIRALING = "spiraling"
    CONCENTRIC = "concentric"
    LINES = "lines"
    CROSSHATCH = "crosshatch"
    ZIGZAG = "zigzag"
    GLYPH_FILL = "glyph_fill"


class ContourMode(str, Enum):
    CENTERLINE = "centerline"
    """The pen straddles the true outline (default) — half the stroke bleeds outward."""
    OUTER = "outer"
    """The pen's outer edge traces the true outline instead of its center —
    offset inward by pen_width/2, same tangency principle as spiraling's
    own ring 0 — so nothing bleeds past the glyph's true boundary."""


def contour_geometry(polygon: BaseGeometry | None, mode: ContourMode, pen_width: float) -> BaseGeometry | None:
    """The geometry whose boundary should be traced for a glyph's outline pass."""
    if polygon is None or polygon.is_empty:
        return None
    if mode == ContourMode.CENTERLINE:
        return polygon
    if mode == ContourMode.OUTER:
        eroded = polygon.buffer(-pen_width / 2, join_style=_JOIN_ROUND, quad_segs=_BUFFER_RESOLUTION)
        return eroded if not eroded.is_empty else None
    raise ValueError(f"Unknown contour mode: {mode}")


@dataclass
class HatchParams:
    fill_type: FillType = FillType.SPIRALING
    spacing: float = 1.0
    """Ring/row spacing for the *pattern* fills — lines, crosshatch and
    concentric — where the spacing is the look and gaps are intended."""
    inset: float = 0.0
    angle: float = 45.0
    pen_width: float = 0.3
    merge_ends: bool = True
    zigzag_passes: int = 1
    """Number of zigzag passes (1-3), evenly spread across 180 degrees. Only used by FillType.ZIGZAG."""
    merge_tolerance: float = 0.0
    """How far a merge-ends bridge is allowed to stray outside the glyph's
    ink area before it's rejected (ends that stroke and starts a new one
    instead). 0 keeps bridges strictly inside; raising it trades some risk
    of a bridge crossing a thin gap for fewer pen lifts / more continuous
    strokes."""
    fill_spacing: float | None = None
    """Line-to-line spacing for the *coverage* fills — spiraling, zigzag and
    glyph_fill — which is a different quantity from ``spacing`` above: it is
    tied to the pen, not to a pattern. ``None`` picks the spacing for you,
    and what it picks depends on ``guarantee_coverage``: one full
    ``pen_width`` (adjacent strokes exactly tangent, which is what makes
    those modes fill solid) when it's on, or ``pen_width *
    _OPEN_SPACING_FACTOR`` when it's off — see ``guarantee_coverage`` for
    why. Set this explicitly to override either default: smaller lays ink
    down heavier (strokes overlap), larger opens the fill up further — but
    with ``guarantee_coverage`` on, the top-up pass will simply re-cover
    whatever a wider spacing opened, since as far as it can tell that's a gap
    the pattern left by accident rather than one asked for. Turn
    ``guarantee_coverage`` off to actually get the opened-up, faster-plotting
    pattern a wider spacing sets out to produce."""
    guarantee_coverage: bool = True
    """Whether the coverage fills (spiraling, zigzag, glyph_fill, and
    concentric when its spacing is tangent-or-tighter than the pen) run their
    top-up pass at all. On by default, which is what makes those fills solid:
    top-up measures whatever the primary pattern missed and covers exactly
    that, regardless of why it was missed. That "regardless of why" is the
    catch — it also erases any gap ``fill_spacing`` opened up on purpose, so
    a spacing set wide for a quicker, more open pattern still plots as a
    solid fill, with all the extra strokes that took. Turn this off to skip
    top-up and let the pattern's own spacing stand: coverage is then whatever
    the primary pattern's spacing naturally gives it, no longer guaranteed
    complete, in exchange for a shorter, sparser plot when that's the goal.

    Turning this off changes nothing to look at, on its own, if
    ``fill_spacing`` is still at its default: tangent spacing is already
    ~solid, so there is nothing for top-up to have been adding back. That is
    why leaving ``fill_spacing`` at ``None`` picks a wider default spacing
    here too — so the checkbox has something to open up by itself, without
    also requiring ``fill_spacing`` to be set by hand. An explicit
    ``fill_spacing`` always wins over both defaults."""


def hatch_polygon(polygon: BaseGeometry | None, params: HatchParams) -> list[LineString]:
    """Fill strokes for one glyph polygon. Does not include the outline itself."""
    if polygon is None or polygon.is_empty:
        return []
    pieces = iter_polygons(polygon)
    if not pieces:
        return []

    tol = params.merge_tolerance
    guarantee = params.guarantee_coverage
    if params.fill_spacing:
        step = params.fill_spacing
    elif guarantee:
        step = params.pen_width
    else:
        step = params.pen_width * _OPEN_SPACING_FACTOR
    if params.fill_type == FillType.SPIRALING:
        return _spiraling(pieces, params.pen_width, step, params.inset, params.merge_ends, tol, guarantee)
    if params.fill_type == FillType.CONCENTRIC:
        return _concentric(pieces, params.pen_width, params.spacing, params.inset, params.merge_ends, tol, guarantee)
    if params.fill_type == FillType.LINES:
        return _scanline_fill(pieces, params.angle, params.spacing, params.inset, params.merge_ends, tol)
    if params.fill_type == FillType.CROSSHATCH:
        lines1 = _scanline_fill(pieces, params.angle, params.spacing, params.inset, params.merge_ends, tol)
        lines2 = _scanline_fill(pieces, params.angle + 90, params.spacing, params.inset, params.merge_ends, tol)
        return lines1 + lines2
    if params.fill_type == FillType.ZIGZAG:
        passes = max(1, min(3, params.zigzag_passes))
        strokes: list[LineString] = []
        for i in range(passes):
            pass_angle = params.angle + i * (180.0 / passes)
            strokes.extend(_zigzag(pieces, params.pen_width, pass_angle, step, params.inset, tol, guarantee))
        return strokes
    if params.fill_type == FillType.GLYPH_FILL:
        return _glyph_fill(pieces, params.pen_width, step, params.inset, tol, guarantee)
    raise ValueError(f"Unknown fill type: {params.fill_type}")


def iter_polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(iter_polygons(g))
        return out
    return []


# --- measuring the ink a stroke actually deposits -------------------------


def _stroke_ink(line: LineString, radius: float) -> BaseGeometry:
    """The area a pen of ``2 * radius`` width inks while drawing ``line``.

    Buffered in short chunks rather than in one call: a threaded spiral is a
    self-intersecting path, and GEOS's buffer() returns an invalid,
    area-inflated shell for those — badly enough to report a letter's counter
    as inked when no stroke crosses it (see :data:`_INK_CHUNK_POINTS`).
    Every gap-detection decision in this module rests on this measurement, so
    it has to be the honest one; a fill that "covers" only because its own
    yardstick over-reports would plot with real holes in it. That is also why
    the arc resolution here matches the one used to *build* the geometry
    rather than a cheaper approximation: at quad_segs=8 the coarser round
    joins quietly closed over thin crescents at ring corners, and the top-up
    pass then never saw the ~0.2mm² of gaps they were hiding."""
    coords = list(line.coords)
    if len(coords) < 2:
        return Polygon()
    parts = [
        LineString(chunk).buffer(radius, join_style=_JOIN_ROUND, quad_segs=_BUFFER_RESOLUTION)
        for i in range(0, len(coords) - 1, _INK_CHUNK_POINTS)
        if len(chunk := coords[i : i + _INK_CHUNK_POINTS + 1]) >= 2
    ]
    return shapely.union_all(parts, grid_size=1e-9) if parts else Polygon()


def _ink_of(lines: list[LineString], pen_width: float) -> BaseGeometry:
    if not lines:
        return Polygon()
    return shapely.union_all([_stroke_ink(line, pen_width / 2) for line in lines], grid_size=1e-9)


# --- offset rings ---------------------------------------------------------


def _erode(geom: BaseGeometry, distance: float) -> BaseGeometry:
    if not distance:
        return geom
    return geom.buffer(-distance, join_style=_JOIN_ROUND, quad_segs=_BUFFER_RESOLUTION)


def _simplify(poly: Polygon, tolerance: float) -> Polygon:
    """Each round-join buffer() re-tessellates its arcs from scratch, so
    vertex count roughly doubles per successive erosion step (observed:
    ~660 points on ring 0 growing past 20,000 by ring 6-7 on an ordinary
    letter) — simplifying after every step keeps it bounded, since that
    level of precision is meaningless at these scales anyway."""
    simplified = poly.simplify(tolerance, preserve_topology=True)
    return simplified if isinstance(simplified, Polygon) and not simplified.is_empty else poly


def _erosion_ring_steps(start_poly: Polygon, ring_offset: float, ring_spacing: float):
    """Yield successive lists of (still eroding) polygon pieces, one list per ring."""
    simplify_tol = max(ring_spacing * 0.05, 1e-4)
    current = iter_polygons(_erode(start_poly, ring_offset)) if ring_offset > 0 else [start_poly]
    current = [_simplify(p, simplify_tol) for p in current]
    ring_index = 0
    while current and ring_index < _MAX_RINGS:
        yield current
        next_pieces: list[Polygon] = []
        for piece in current:
            next_pieces.extend(_simplify(p, simplify_tol) for p in iter_polygons(_erode(piece, ring_spacing)))
        current = next_pieces
        ring_index += 1


def _ring_loops(pieces: list[Polygon]) -> list[tuple[LinearRing, str]]:
    """Loops tagged 'ext'/'int' — an exterior boundary shrinks over successive
    rings while an interior (hole) boundary grows, so on a symmetric shape
    they can share a centroid; the tag keeps chain-matching from confusing
    the two rather than relying on position alone."""
    loops: list[tuple[LinearRing, str]] = []
    for p in pieces:
        loops.append((p.exterior, "ext"))
        loops.extend((interior, "int") for interior in p.interiors)
    return loops


def _ring_chains(poly: Polygon, ring_offset: float, ring_spacing: float) -> list[list[LinearRing]]:
    """Group the offset rings into nested chains — which ring "continues"
    which — by nearest-centroid matching between consecutive erosion steps.
    A stroke can split into disconnected pieces as it thins; a split simply
    ends one chain and starts new ones."""
    threshold = ring_spacing * 2.5
    all_chains: list[list[LinearRing]] = []
    open_chains: dict[int, list[LinearRing]] = {}
    open_kinds: dict[int, str] = {}
    next_id = 0

    for pieces in _erosion_ring_steps(poly, ring_offset, ring_spacing):
        loops = _ring_loops(pieces)
        if not loops:
            continue
        if not open_chains:
            for loop, kind in loops:
                chain = [loop]
                all_chains.append(chain)
                open_chains[next_id] = chain
                open_kinds[next_id] = kind
                next_id += 1
            continue

        candidates = []
        for cid, chain in open_chains.items():
            prev_centroid = chain[-1].centroid
            prev_kind = open_kinds[cid]
            for li, (loop, kind) in enumerate(loops):
                if kind == prev_kind:
                    candidates.append((prev_centroid.distance(loop.centroid), cid, li))
        candidates.sort(key=lambda t: t[0])

        used_cid: set[int] = set()
        used_li: set[int] = set()
        matched: dict[int, int] = {}
        for dist, cid, li in candidates:
            if dist > threshold:
                break
            if cid in used_cid or li in used_li:
                continue
            used_cid.add(cid)
            used_li.add(li)
            matched[li] = cid

        new_open: dict[int, list[LinearRing]] = {}
        new_kinds: dict[int, str] = {}
        for li, (loop, kind) in enumerate(loops):
            if li in matched:
                cid = matched[li]
                open_chains[cid].append(loop)
                new_open[cid] = open_chains[cid]
                new_kinds[cid] = kind
            else:
                chain = [loop]
                all_chains.append(chain)
                new_open[next_id] = chain
                new_kinds[next_id] = kind
                next_id += 1
        open_chains = new_open
        open_kinds = new_kinds

    return all_chains


def _contour_lines(piece: Polygon, pen_width: float) -> list[LineString]:
    """The paths an ``OUTER`` contour pass lays down for this piece.

    The coverage fills count this as ink already on the paper when they work
    out what they still have to cover. That is not an optimisation detail but
    the division of labour: ``draw_contour`` is on by default and
    ``ContourMode.OUTER`` traces exactly one pen width inside the true edge,
    so a fill that also covered the boundary band would simply be drawing it
    twice. For ``spiraling`` the point is moot — its ring 0 runs along that
    same line — but for ``zigzag``, whose rows are straight chords, the
    difference is the scalloped sliver between the end of one row and the
    next, and chasing those cost ~1000mm of extra stroke on a five-letter word
    to re-ink what the contour had already covered.

    A caller who turns the contour off gets the boundary band from neither, so
    ``--no-draw-contour`` with these fills leaves the outermost sliver of the
    letterform bare by design.
    """
    contour = contour_geometry(piece, ContourMode.OUTER, pen_width)
    lines: list[LineString] = []
    for poly in iter_polygons(contour) if contour is not None else ():
        lines.append(LineString(poly.exterior.coords))
        lines.extend(LineString(interior.coords) for interior in poly.interiors)
    return lines


# --- spiraling ------------------------------------------------------------


def _oriented(ring: LinearRing, ccw: bool = True) -> LinearRing:
    return LinearRing(list(ring.coords)[::-1]) if ring.is_ccw != ccw else ring


def _roll_to(ring: LinearRing, point) -> np.ndarray:
    """Ring coordinates rotated to begin at the point on ``ring`` nearest
    ``point``, closed.

    The seam is inserted by projecting ``point`` onto the ring (continuous
    arc length via ``project``/``interpolate``) rather than snapping to
    whichever existing vertex is closest: on a long straight edge — a square,
    a letter's stem — many vertices sit at nearly the same distance from a
    given ``point``, so nearest-*vertex* jumps unpredictably between
    revolutions and each jump is a visible lateral notch cut across the
    ribbon. Projecting first gives a single, continuous seam position, and
    only that seam point (not the ring's own vertices) needs inserting into
    the coordinate list."""
    coords = np.asarray(ring.coords)[:-1]
    if len(coords) == 0:
        return coords
    seam = ring.interpolate(ring.project(Point(point)))
    d2 = (coords[:, 0] - seam.x) ** 2 + (coords[:, 1] - seam.y) ** 2
    k = int(np.argmin(d2))
    seam_xy = np.array([[seam.x, seam.y]])
    if d2[k] < 1e-12:
        return np.vstack([coords[k:], coords[:k], coords[k : k + 1]])
    # k is the vertex right after the seam along the ring's direction (or
    # right before it — both give a valid split, ``argmin`` just picks
    # whichever is numerically closer); either way the seam itself still
    # needs inserting so the path actually starts there, not at a neighbour.
    return np.vstack([seam_xy, coords[k:], coords[:k], seam_xy])


def _spiral_from_chain(chain: list[LinearRing], ring_spacing: float) -> list[LineString]:
    """Thread nested rings into as few continuous paths as possible.

    Each ring is traced in full and the path then hops to the nearest point
    of the next ring — one short radial jog per revolution, about
    ``ring_spacing`` long. Because every ring is traversed completely,
    coverage is exactly the rings' own coverage.

    Morphing consecutive rings into a seam-free spiral (blending ring i into
    ring i+1 across one revolution) was tried again here and reverted again:
    it reads as a genuine spiral on gently-curved letterforms, but on any
    shape with long straight runs (a square annulus, a straight stem) a
    per-index lerp between two arc-length-resampled rings does not track the
    true offset — it cuts a visible comb of teeth across the ribbon, and on
    one test shape left ~19% of the area uncovered even with :func:`_topup`
    running afterwards (a self-intersecting-enough path that the same
    ink-measurement GEOS problem :data:`_INK_CHUNK_POINTS` exists for elsewhere
    in this file quietly under-reports the gap). Tracing each ring in full
    sidesteps that class of failure entirely: whatever :func:`_topup` measures
    as missing here is genuinely missing, not a measurement artefact of the
    stroke's own shape. The jog is a much cheaper artefact than a hole."""
    rings = [_oriented(r) for r in chain if r.length > 0]
    if not rings:
        return []
    strokes: list[LineString] = []
    path: np.ndarray | None = None
    for ring in rings:
        if path is None:
            path = _roll_to(ring, ring.coords[0])
            continue
        nxt = _roll_to(ring, path[-1])
        if len(nxt) and math.dist(nxt[0], path[-1]) <= ring_spacing * _SPIRAL_JOG_FACTOR:
            path = np.vstack([path, nxt])
        else:
            if len(path) >= 2:
                strokes.append(LineString(path))
            path = nxt
    if path is not None and len(path) >= 2:
        strokes.append(LineString(path))
    return strokes


def _spiraling(
    pieces: list[Polygon],
    pen_width: float,
    ring_spacing: float,
    inset: float,
    merge_ends: bool,
    tol: float,
    guarantee_coverage: bool = True,
) -> list[LineString]:
    strokes: list[LineString] = []
    for piece in pieces:
        chains = _ring_chains(piece, pen_width / 2 + inset, ring_spacing)
        piece_strokes: list[LineString] = []
        for chain in chains:
            if merge_ends:
                piece_strokes.extend(_spiral_from_chain(chain, ring_spacing))
            else:
                piece_strokes.extend(LineString(loop.coords) for loop in chain)
        if guarantee_coverage:
            piece_strokes.extend(_topup(piece, piece_strokes + _contour_lines(piece, pen_width), pen_width))
        if merge_ends:
            piece_strokes = _thread(piece_strokes, piece, max_bridge=pen_width * _GLYPH_FILL_MAX_BRIDGE, tol=tol, pen_width=pen_width)
        strokes.extend(piece_strokes)
    return strokes


# --- concentric -----------------------------------------------------------


def _concentric(
    pieces: list[Polygon],
    pen_width: float,
    spacing: float,
    inset: float,
    merge_ends: bool,
    tol: float,
    guarantee_coverage: bool = True,
) -> list[LineString]:
    """Outside-in closed loops: the glyph's own outline repeated inward as
    many times as it fits.

    Normally a pattern rather than a solid fill — the gaps between rings are
    the look — so ring 0 sits one ``spacing`` inside the OUTER contour (which
    already draws the outermost loop) and nothing tops up what the rings
    leave bare. But a caller who sets ``spacing`` at or below ``pen_width``
    has asked for tangent or overlapping rings, i.e. solid coverage, not a
    pattern; that case starts at ``pen_width / 2`` like ``spiraling`` and
    gets the same top-up, so "concentric with spacing == pen_width" still
    means a filled glyph."""
    solid = pen_width >= spacing
    ring_offset = (pen_width / 2 if solid else pen_width / 2 + spacing) + inset
    strokes: list[LineString] = []
    for piece in pieces:
        rings: list[LineString] = []
        for level in _erosion_ring_steps(piece, ring_offset, spacing):
            for poly in level:
                for ring in (poly.exterior, *poly.interiors):
                    if ring.length > spacing * _CONCENTRIC_MIN_RING_FACTOR:
                        rings.append(LineString(ring.coords))
        if solid and guarantee_coverage:
            rings.extend(_topup(piece, rings, pen_width))
        if merge_ends and rings:
            strokes.extend(_thread(rings, piece, max_bridge=spacing * 4, tol=tol, pen_width=pen_width))
        else:
            strokes.extend(rings)
    return strokes


# --- zigzag ---------------------------------------------------------------


def _row_spans(domain: Polygon, angle: float, spacing: float):
    """Per-row spans in the frame rotated so rows are horizontal.
    Returns ``(rows, centroid, rotated_domain)``; each row is a list of
    ``(x0, x1, y)`` sorted left to right."""
    centroid = domain.centroid
    rotated = shapely_rotate(domain, -angle, origin=centroid, use_radians=False)
    minx, miny, maxx, maxy = rotated.bounds
    pad = max(maxx - minx, maxy - miny) * 0.01 + 1e-6
    # The domain is already eroded by pen_width/2, so its own edge is exactly
    # where the first row's *centre* belongs: that puts the pen's outer edge
    # tangent to the true outline, the same tangency spiraling's ring 0 uses.
    # Starting half a spacing in instead leaves a bare half-pen band all round
    # the glyph (and starting on the un-eroded polygon, as this used to, spills
    # a half-pen halo outside it).
    ys = []
    y = miny
    while y <= maxy:
        ys.append(y)
        y += spacing
    if ys and maxy - ys[-1] > spacing * 0.25:
        ys.append(maxy)      # keep the far edge tangent too
    if not ys and maxy >= miny:
        ys = [(miny + maxy) / 2]
    rows = []
    for y in ys:
        clipped = rotated.intersection(LineString([(minx - pad, y), (maxx + pad, y)]))
        spans = []
        for g in _as_segments(clipped):
            x0, x1 = sorted((g[0][0], g[1][0]))
            if x1 - x0 > 1e-12:
                spans.append((x0, x1, y))
        if spans:
            spans.sort()
            rows.append(spans)
    return rows, centroid, rotated


def _zigzag(
    pieces: list[Polygon],
    pen_width: float,
    angle: float,
    spacing: float,
    inset: float,
    tol: float,
    guarantee_coverage: bool = True,
) -> list[LineString]:
    """``lines``, but continuous: one long back-and-forth stroke.

    A plain serpentine breaks the moment a row splits in two — which on
    letterforms is most of them ('u', 't', the waist of an 'S'). So spans are
    walked as a graph instead: from the end of the current span, step to an
    unvisited span in an adjacent row that overlaps it and whose connector
    stays inside the shape, entering at the near end and leaving at the far
    end so the direction alternates by itself. That keeps the pen down across
    a branch and back, and only lifts when nothing reachable is left.

    The domain is eroded by ``pen_width / 2`` first so rows stop where the
    pen's *edge* meets the outline rather than its centre — without that, a
    fill that looks correct deposits a half-pen halo all round the glyph
    (measured at ~10% of the letterform's own area spilled)."""
    strokes: list[LineString] = []
    for piece in pieces:
        for domain in iter_polygons(_erode(piece, pen_width / 2 + inset)):
            rows, centroid, rotated = _row_spans(domain, angle, spacing)
            if not rows:
                continue
            spans: list[list] = []
            by_row: dict[int, list[int]] = {}
            for ri, row in enumerate(rows):
                for x0, x1, y in row:
                    by_row.setdefault(ri, []).append(len(spans))
                    spans.append([ri, x0, x1, y, False])

            reach = rotated.buffer(max(tol, 1e-9))
            paths: list[LineString] = []
            while True:
                start = next((i for i, s in enumerate(spans) if not s[4]), None)
                if start is None:
                    break
                spans[start][4] = True
                _, x0, x1, y, _ = spans[start]
                pts = [(x0, y), (x1, y)]
                head = (x1, y)
                cur = start
                while True:
                    ri, x0, x1 = spans[cur][0], spans[cur][1], spans[cur][2]
                    best = None
                    for nri in (ri + 1, ri - 1):
                        for j in by_row.get(nri, ()):
                            if spans[j][4]:
                                continue
                            _, a0, a1, ay, _ = spans[j]
                            if min(x1, a1) - max(x0, a0) <= -spacing:
                                continue  # not overlapping enough to be the same lane
                            for entry in ((a0, ay), (a1, ay)):
                                d = math.dist(head, entry)
                                if best is not None and d >= best[0]:
                                    continue
                                if reach.contains(LineString([head, entry])):
                                    best = (d, j, entry, (a1, ay) if entry == (a0, ay) else (a0, ay))
                    if best is None:
                        break
                    _, j, entry, other = best
                    spans[j][4] = True
                    pts += [entry, other]
                    head = other
                    cur = j
                if len(pts) >= 2:
                    paths.append(LineString(pts))
            unrotated = [shapely_rotate(p, angle, origin=centroid, use_radians=False) for p in paths]
            if guarantee_coverage:
                unrotated.extend(_topup(piece, unrotated + _contour_lines(piece, pen_width), pen_width))
            strokes.extend(_thread(unrotated, piece, max_bridge=pen_width * _GLYPH_FILL_MAX_BRIDGE, tol=tol, pen_width=pen_width))
    return strokes


# --- glyph_fill -----------------------------------------------------------


def _glyph_fill(
    pieces: list[Polygon],
    pen_width: float,
    ring_spacing: float,
    inset: float,
    tol: float,
    guarantee_coverage: bool = True,
) -> list[LineString]:
    """Contour-led fill: only what the OUTER contour could not reach.

    The contour already inks a full pen width in from the true edge, so the
    rings start a further ``pen_width`` in (ring 0 of ``spiraling`` would
    just retrace the contour) and the coverage check counts the contour's own
    ink alongside them. On lettering small enough that the contour already
    covers a stem end to end, that leaves nothing to draw — and this mode
    correctly returns no strokes for it at all."""
    strokes: list[LineString] = []
    for piece in pieces:
        contour_lines = _contour_lines(piece, pen_width)
        spacing = ring_spacing * _GLYPH_FILL_OVERLAP
        piece_strokes: list[LineString] = []
        for chain in _ring_chains(piece, pen_width + spacing / 2 + inset, spacing):
            piece_strokes.extend(_spiral_from_chain(chain, spacing))
        if guarantee_coverage:
            piece_strokes.extend(_topup(piece, piece_strokes + contour_lines, pen_width))
        if piece_strokes:
            strokes.extend(_thread(piece_strokes, piece, max_bridge=pen_width * _GLYPH_FILL_MAX_BRIDGE, tol=tol, pen_width=pen_width))
    return strokes


# --- top-up: cover what the pattern missed --------------------------------


def _fill_angle(poly: Polygon, spacing: float) -> float:
    """The direction to run fill lines in, for one leftover patch: whichever
    of a handful of candidate angles covers it in the fewest separate lines.

    The tempting answer is the long side of the tightest enclosing rectangle,
    and it is wrong for exactly the shapes that turn up here. What offset
    rings leave behind is a ribbon along the medial axis, and a ribbon that
    *bends* — the inside of a 'u', the waist of an 'S' — has a nearly square
    bounding rectangle, so that rule picks an essentially arbitrary angle. Get
    it wrong by 90 degrees and every line runs across the stem instead of
    along it, ending on the outline at both ends, which is how a fill that
    measured as full coverage still laid a scalloped halo of overhang around
    the whole letterform.

    Counting lines instead is both robust to that and the thing actually worth
    optimising: running along the ribbon yields a few long strokes, running
    across it yields many short ones, and fewer strokes is the goal in its own
    right. Ties are broken toward the shorter total, and the candidate set is
    coarse on purpose — this runs per leftover patch, and the difference
    between the best angle and one a few degrees off is not worth the cost of
    finding it."""
    if poly.is_empty or poly.area <= 0:
        return 0.0
    best_angle, best_key = 0.0, None
    for i in range(12):
        angle = i * 15.0
        segments = _scan_segments(poly, angle, spacing)
        if not segments:
            continue
        key = (len(segments), sum(s.length for s in segments))
        if best_key is None or key < best_key:
            best_key, best_angle = key, angle
    return best_angle


def _scan_segments(poly: Polygon, angle: float, spacing: float) -> list[LineString]:
    """Plain scanlines at ``angle``, unmerged."""
    if poly.is_empty or spacing <= 0:
        return []
    centroid = poly.centroid
    rotated = shapely_rotate(poly, -angle, origin=centroid, use_radians=False)
    minx, miny, maxx, maxy = rotated.bounds
    pad = max(maxx - minx, maxy - miny) * 0.01 + 1e-6
    out: list[LineString] = []
    y = miny + spacing / 2
    if y > maxy and maxy > miny:
        y = (miny + maxy) / 2
    while y <= maxy:
        clipped = rotated.intersection(LineString([(minx - pad, y), (maxx + pad, y)]))
        for p0, p1 in _as_segments(clipped):
            out.append(shapely_rotate(LineString([p0, p1]), angle, origin=centroid, use_radians=False))
        y += spacing
    return out


def _lengthen(line: LineString, minimum: float, within: BaseGeometry) -> LineString:
    """Grow a stroke from both ends up to ``minimum``, staying in ``within``.

    A gap narrower than the nib yields a scan segment a fraction of a
    millimetre long — which is not a stroke, it is a dab, and it plots as a
    blot with a pen lift on either side. Stretching it along its own
    direction to at least one pen width costs no extra pen lift, covers the
    same gap with margin, and gives the plotter something it can actually
    draw."""
    if line.length >= minimum:
        return line
    coords = list(line.coords)
    if len(coords) < 2:
        return line
    (x0, y0), (x1, y1) = coords[0], coords[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy)
    if norm <= 0:
        return line
    grow = (minimum - line.length) / 2 + 1e-9
    ux, uy = dx / norm, dy / norm
    stretched = LineString([(x0 - ux * grow, y0 - uy * grow), (x1 + ux * grow, y1 + uy * grow)])
    clipped = stretched.intersection(within)
    parts = [clipped] if clipped.geom_type == "LineString" else [
        g for g in getattr(clipped, "geoms", ()) if g.geom_type == "LineString"
    ]
    parts = [g for g in parts if g.length >= line.length]
    return max(parts, key=lambda g: g.length) if parts else line


def _topup(glyph: Polygon, existing: list[LineString], pen_width: float) -> list[LineString]:
    """Cover whatever ``existing`` genuinely missed.

    What offset rings leave behind is a thin ribbon along the shape's medial
    axis — where the erosion ran out before the two sides met. Each ribbon is
    therefore scanned *along* its own long axis, so anything narrower than
    the pen becomes a single centreline stroke. Scanning across it instead
    (the obvious reading of "fill this leftover polygon") is what turns a
    handful of gaps into dozens of stubs, each with its own pen lift: on
    'Salut' that was ~130 extra strokes for the same coverage.

    Where the gap is in a feature thinner than the pen, the no-spill domain
    is empty and the constraint is relaxed in turn — see
    :data:`_TOPUP_DOMAIN_STEPS`."""
    ink = _ink_of(existing, pen_width)
    remainder = glyph.difference(ink) if not ink.is_empty else glyph
    if remainder.is_empty:
        return []

    # Relaxing the no-spill domain (see _TOPUP_DOMAIN_STEPS) is for features
    # narrower than the pen but still worth inking. When the *whole* shape is
    # smaller than a single dab of the pen there is no such feature — marking
    # it would deposit far more ink outside the shape than in — so only the
    # strict domain applies, and if that is empty this shape simply cannot be
    # filled by this pen.
    steps = _TOPUP_DOMAIN_STEPS if glyph.area >= pen_width**2 else _TOPUP_DOMAIN_STEPS[:1]
    domains = [d for frac in steps if not (d := _erode(glyph, pen_width * frac)).is_empty]
    if not domains:
        return []
    strict = _erode(glyph, pen_width / 2)

    min_area = (pen_width**2) * _TOPUP_MIN_AREA_FRACTION
    out: list[LineString] = []
    for patch in iter_polygons(remainder):
        if patch.area < min_area:
            continue
        # Relax the no-spill domain only where the letterform is genuinely
        # narrower than the pen, judged locally: take the patch's own
        # neighbourhood within the glyph and ask whether *anything* survives
        # eroding it by half a nib.
        #
        # The case this has to tell apart is a sharp convex corner. A round
        # nib cannot ink into a square corner — its ink stops a radius short,
        # leaving a small patch the strict domain can never reach either — and
        # that is not a defect to be fixed, it is the shape of the tool.
        # Chasing it means walking the pen centre out onto the outline, which
        # both overhangs and rounds the corner off in the opposite direction.
        # On 12mm text that chase cost ~19% of each letterform's area in
        # spilled ink to fill corners that the eye reads as sharp either way.
        # A feature thinner than the nib is the opposite case: leaving it
        # blank loses the stroke altogether, so there the overhang is worth it.
        neighbourhood = patch.buffer(pen_width, join_style=_JOIN_ROUND, quad_segs=_BUFFER_RESOLUTION).intersection(
            glyph
        )
        thin = _erode(neighbourhood, pen_width / 2).is_empty
        usable = domains if thin else domains[:2]

        left: BaseGeometry = patch
        for index, domain in enumerate(usable):
            grown = left.buffer(pen_width * _TOPUP_DILATE, join_style=_JOIN_ROUND, quad_segs=_BUFFER_RESOLUTION)
            target = grown.intersection(domain)
            if target.is_empty:
                continue
            segments: list[LineString] = []
            for part in iter_polygons(target):
                spacing = pen_width * _TOPUP_SPACING
                segments.extend(_scan_segments(part, _fill_angle(part, spacing), spacing))
            segments = [
                _lengthen(_lengthen(g, pen_width, domain), pen_width, glyph if thin else domain)
                for g in segments
                if g.length > 1e-9
            ]
            if not segments:
                continue
            out.extend(segments)
            left = left.difference(_ink_of(segments, pen_width))
            if left.is_empty or left.area < min_area:
                break
    return [s for s in out if s.length > 1e-9]


# --- threading: fewest strokes, shortest travel ---------------------------


def _thread(
    strokes: list[LineString],
    glyph: Polygon,
    max_bridge: float | None = None,
    tol: float = 0.0,
    pen_width: float = 0.0,
) -> list[LineString]:
    """Chain strokes greedily by nearest endpoint.

    A connector that stays inside the glyph is absorbed into the path — the
    pen stays down and the stroke count drops. One that would cross a counter
    (or the gap between the dot and stem of an 'i'), or that is simply too
    far to be worth it, ends the stroke instead. Either way the next stroke
    is *chosen* by proximity, so even un-merged output leaves the shortest
    pen-up travel this is cheaply able to find.

    Both ends of the growing chain are considered, not just the back: once a
    few strokes have merged, the chain's start is buried inside its own
    coordinate list, and something close only to *that* end would otherwise
    never be considered again.

    A connector shorter than half a pen width is taken without the
    stays-inside test. At that range the two endpoints are inside the same
    dab of ink the nib is laying down regardless, so the connector cannot
    put ink anywhere the pen was not already going to put it — and refusing
    it would spend a whole pen lift to avoid a move smaller than the nib.

"""
    strokes = [s for s in strokes if s.length > 1e-9]
    if len(strokes) <= 1:
        return strokes
    inside = glyph.buffer(max(tol, 1e-9))
    limit = math.inf if max_bridge is None else max_bridge

    remaining = [list(s.coords) for s in strokes]
    out: list[LineString] = []
    current = remaining.pop(0)
    while remaining:
        front, back = current[0], current[-1]
        best = None
        for i, coords in enumerate(remaining):
            for reversed_ in (False, True):
                candidate = coords[::-1] if reversed_ else coords
                d_back = math.dist(back, candidate[0])
                if best is None or d_back < best[0]:
                    best = (d_back, i, reversed_, True)
                d_front = math.dist(front, candidate[-1])
                if d_front < best[0]:
                    best = (d_front, i, reversed_, False)
        dist, idx, reversed_, at_back = best
        candidate = remaining[idx][::-1] if reversed_ else remaining[idx]
        join_point = back if at_back else front
        other_point = candidate[0] if at_back else candidate[-1]
        negligible = max(pen_width / 2, 1e-9)
        if dist <= limit and (dist <= negligible or inside.contains(LineString([join_point, other_point]))):
            current = current + candidate if at_back else candidate + current
            remaining.pop(idx)
        else:
            out.append(LineString(current))
            current = remaining.pop(idx)
    out.append(LineString(current))
    return out


# --- lines / crosshatch: scanline fill ------------------------------------


def _scanline_fill(
    polygons: list[Polygon],
    angle: float,
    spacing: float,
    inset: float,
    merge_ends: bool,
    merge_tolerance: float = 0.0,
) -> list[LineString]:
    strokes: list[LineString] = []
    for poly in polygons:
        shrunk = _erode(poly, inset) if inset > 0 else poly
        for piece in iter_polygons(shrunk):
            strokes.extend(_scanline_fill_single(piece, angle, spacing, merge_ends, merge_tolerance))
    return strokes


def _scanline_fill_single(
    poly: Polygon, angle: float, spacing: float, merge_ends: bool, merge_tolerance: float = 0.0
) -> list[LineString]:
    if poly.is_empty or spacing <= 0:
        return []

    centroid = poly.centroid
    rotated = shapely_rotate(poly, -angle, origin=centroid, use_radians=False)
    minx, miny, maxx, maxy = rotated.bounds
    pad = max(maxx - minx, maxy - miny) * 0.01 + 1e-6

    rows: list[list[tuple[tuple[float, float], tuple[float, float]]]] = []
    y = miny + spacing / 2
    while y <= maxy:
        scan = LineString([(minx - pad, y), (maxx + pad, y)])
        segments = _as_segments(rotated.intersection(scan))
        if segments:
            segments.sort(key=lambda seg: min(seg[0][0], seg[1][0]))
            rows.append(segments)
        y += spacing

    if not rows:
        return []

    if not merge_ends:
        result = []
        for row in rows:
            for p0, p1 in row:
                result.append(_rotate_back(LineString([p0, p1]), angle, centroid))
        return result

    strokes: list[LineString] = []
    path: list[tuple[float, float]] = []
    boundary_buffered: BaseGeometry | None = None
    for row_index, row in enumerate(rows):
        ordered = row if row_index % 2 == 0 else list(reversed(row))
        for p0, p1 in ordered:
            if not path:
                path.extend([p0, p1])
                continue
            last = path[-1]
            d_p0 = (last[0] - p0[0]) ** 2 + (last[1] - p0[1]) ** 2
            d_p1 = (last[0] - p1[0]) ** 2 + (last[1] - p1[1]) ** 2
            near, far = (p0, p1) if d_p0 <= d_p1 else (p1, p0)
            if boundary_buffered is None:
                boundary_buffered = rotated.buffer(max(merge_tolerance, 1e-9))
            if boundary_buffered.contains(LineString([last, near])):
                path.extend([near, far])
            else:
                if len(path) >= 2:
                    strokes.append(LineString(path))
                path = [near, far]
    if len(path) >= 2:
        strokes.append(LineString(path))

    return [_rotate_back(s, angle, centroid) for s in strokes]


def _as_segments(geom: BaseGeometry) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        coords = list(geom.coords)
        return [(coords[0], coords[-1])] if len(coords) >= 2 else []
    if isinstance(geom, MultiLineString) or geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_as_segments(g))
        return out
    return []


def _rotate_back(geom: BaseGeometry, angle: float, origin) -> LineString:
    return shapely_rotate(geom, angle, origin=origin, use_radians=False)
