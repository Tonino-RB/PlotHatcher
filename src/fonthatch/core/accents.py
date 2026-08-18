"""Patch a few of vpype's Hershey fonts with accented glyphs.

The Hershey-family fonts vpype bundles only digitize the 96 printable ASCII
glyphs (index ``ord(char) - 32``, see ``singleline.py``), so accented
letters have no entry and silently vanish. Rather than touch the lookup in
``singleline.py`` (which is deliberately minimal and font-agnostic), this
module extends a handful of fonts' own glyph tables in place — at import
time, before ``singleline.py`` ever calls ``_Font.get()`` for them — so the
existing ``ord(char) - 32`` lookup just finds real data.

Each accented glyph is synthesized from the font's own letterforms: the
base letter's strokes (reused verbatim, so its shape is unchanged) plus a
diacritic mark built by reusing that *same* font's own comma/period glyphs
(for cedilla/diaeresis) or a short tick sized from its cap-height/x-height
gap (for grave/acute) — so marks share the font's own digitization style
and stay proportional across fonts of different scale, rather than being
independently hand-drawn shapes bolted on. Only a few representative fonts
are patched — this is a targeted fix, not a full re-digitization — and
``marked_font_names()`` flags which ones in a font-name listing.
"""

from __future__ import annotations

from vpype.model import LineCollection
from vpype.text import FONT_NAMES, _Font, _Glyph

__all__ = ["marked_font_names", "mark_font_name", "unmark_font_name", "patch_accented_glyphs"]

_PATCHED_FONT_NAMES = (
    "futural",
    "futuram",
    "timesr",
    "timesrb",
    "timesi",
    "timesib",
    "rowmans",
    "rowmand",
)

_ACCENTS: dict[str, tuple[str, str]] = {
    "à": ("a", "grave"),
    "À": ("A", "grave"),
    "è": ("e", "grave"),
    "È": ("E", "grave"),
    "é": ("e", "acute"),
    "É": ("E", "acute"),
    "ë": ("e", "diaeresis"),
    "Ë": ("E", "diaeresis"),
    "ù": ("u", "grave"),
    "Ù": ("U", "grave"),
    "ç": ("c", "cedilla"),
    "Ç": ("C", "cedilla"),
}

_BASELINE_Y = 9.0
"""Baseline y in the raw Hershey digitization, same convention as
``singleline._HERSHEY_BASELINE_Y`` — verified empirically here too (the
y-bottom of 'a'/'A' in every font in ``_PATCHED_FONT_NAMES``)."""


def _ink_bounds(lines: list[list[complex]]) -> tuple[float, float, float]:
    """(min x, max x, min y) of a set of strokes, in raw font units."""
    xs = [p.real for line in lines for p in line]
    ys = [p.imag for line in lines for p in line]
    return min(xs), max(xs), min(ys)


def _centroid(lines: list[list[complex]]) -> complex:
    pts = [p for line in lines for p in line]
    return sum(pts) / len(pts)


def _topmost_point(lines: list[list[complex]]) -> complex:
    return min((p for line in lines for p in line), key=lambda p: p.imag)


def _accent_span(font: _Font, base: _Glyph) -> float:
    """Vertical gap between cap-height top ('A') and this letter's own
    ink top — the natural scale for a diacritic mark in this font. For an
    uppercase base letter (own top == cap top) this collapses to zero, so
    it falls back to a fixed span sized for sitting just above the cap."""
    _, _, own_top = _ink_bounds([list(line) for line in base.lines])
    _, _, cap_top = _ink_bounds([list(line) for line in font.glyphs[ord("A") - 32].lines])
    span = own_top - cap_top
    return span if span > 0 else 7.0


def _diacritic_mark(font: _Font, base: _Glyph, kind: str) -> list[list[complex]]:
    """A short straight tick above the letter: '/' for acute, '\\' for grave."""
    min_x, max_x, own_top = _ink_bounds([list(line) for line in base.lines])
    span = _accent_span(font, base)
    top_y = own_top - 0.85 * span
    bottom_y = own_top - 0.15 * span
    half_w = 0.4 * span
    center_x = (min_x + max_x) / 2

    if kind == "grave":
        return [[complex(center_x - half_w, top_y), complex(center_x + half_w, bottom_y)]]
    if kind == "acute":
        return [[complex(center_x - half_w, bottom_y), complex(center_x + half_w, top_y)]]
    raise ValueError(f"unknown diacritic kind {kind!r}")


def _diaeresis_mark(font: _Font, base: _Glyph) -> list[list[complex]]:
    """Two small dots, built from the font's own period glyph (so their
    size/shape matches how that font already draws a dot), side by side
    above the letter."""
    period_lines = [list(line) for line in font.glyphs[ord(".") - 32].lines]
    period_center = _centroid(period_lines)

    min_x, max_x, own_top = _ink_bounds([list(line) for line in base.lines])
    span = _accent_span(font, base)
    mark_y = own_top - 0.45 * span
    center_x = (min_x + max_x) / 2
    gap = 0.35 * span

    dots: list[list[complex]] = []
    for dx in (-gap, gap):
        shift = complex(center_x + dx, mark_y) - period_center
        for line in period_lines:
            dots.append([p + shift for p in line])
    return dots


def _cedilla_mark(font: _Font, base: _Glyph) -> list[list[complex]]:
    """A hook under the letter's right side, built from the font's own
    comma glyph (reusing its already-good curl instead of a hand-rolled
    one) anchored by its own topmost point onto the baseline."""
    comma_lines = [list(line) for line in font.glyphs[ord(",") - 32].lines]
    comma_top = _topmost_point(comma_lines)

    min_x, max_x, _ = _ink_bounds([list(line) for line in base.lines])
    attach_x = min_x + 0.65 * (max_x - min_x)
    shift = complex(attach_x, _BASELINE_Y) - comma_top
    return [[p + shift for p in line] for line in comma_lines]


def _mark_lines(font: _Font, base: _Glyph, kind: str) -> list[list[complex]]:
    if kind == "cedilla":
        return _cedilla_mark(font, base)
    if kind == "diaeresis":
        return _diaeresis_mark(font, base)
    return _diacritic_mark(font, base, kind)


def _build_accented_glyph(font: _Font, base_char: str, kind: str) -> _Glyph:
    base = font.glyphs[ord(base_char) - 32]
    marks = _mark_lines(font, base, kind)
    return _Glyph(base.lt, base.rt, LineCollection([*base.lines, *marks]))


def _patch_font(font_name: str) -> None:
    font = _Font.get(font_name)
    max_index = max(ord(ch) - 32 for ch in _ACCENTS)

    already_patched = len(font.glyphs) > max_index and len(font.glyphs[max_index].lines) > 0
    if already_patched:
        return

    while len(font.glyphs) <= max_index:
        font.glyphs.append(_Glyph(0.0, 0.0, LineCollection()))

    for accented, (base_char, kind) in _ACCENTS.items():
        font.glyphs[ord(accented) - 32] = _build_accented_glyph(font, base_char, kind)


def patch_accented_glyphs() -> None:
    """Idempotently add accented glyphs to ``_PATCHED_FONT_NAMES``."""
    for font_name in _PATCHED_FONT_NAMES:
        _patch_font(font_name)


def marked_font_names() -> list[str]:
    """``vpype.text.FONT_NAMES``, with a trailing ``*`` on the fonts that
    carry accented glyphs. Use ``unmark_font_name`` to recover the real
    name before passing a value back into ``_Font.get``/singleline.py."""
    return [mark_font_name(name) for name in FONT_NAMES]


def mark_font_name(font_name: str) -> str:
    """Append the ``*`` ``marked_font_names`` uses, if ``font_name`` is
    patched — for redisplaying a real font name (e.g. a stored/default
    value) as one of the choices ``marked_font_names`` produced."""
    return f"{font_name}*" if font_name in _PATCHED_FONT_NAMES else font_name


def unmark_font_name(font_name: str) -> str:
    """Strip the ``*`` added by ``marked_font_names``, if present."""
    return font_name.rstrip("*") if font_name.endswith("*") else font_name


patch_accented_glyphs()
