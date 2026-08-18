from pathlib import Path

from fonthatch.core.font_resolve import (
    ResolvedFont,
    _normalize_style,
    _normalize_weight,
    _split_family_list,
    resolve_fallback_font,
    resolve_font,
)

# DejaVu Sans ships as matplotlib package data, so it resolves identically
# on every OS/CI runner regardless of what's actually installed system-wide
# — unlike "Arial" (used elsewhere in the fixtures), which only works
# because it happens to be present on this dev machine.
_BUNDLED_FAMILY = "DejaVu Sans"


def test_split_family_list_strips_whitespace_and_quotes():
    assert _split_family_list('"Helvetica Neue", Arial, sans-serif') == ["Helvetica Neue", "Arial", "sans-serif"]
    assert _split_family_list("  Arial ,  'Times New Roman'  ") == ["Arial", "Times New Roman"]


def test_split_family_list_empty_input_yields_empty_list():
    assert _split_family_list("") == []
    assert _split_family_list("   ") == []


def test_normalize_weight_accepts_keywords_and_numbers():
    assert _normalize_weight("bold") == "bold"
    assert _normalize_weight("BoLD") == "bold"
    assert _normalize_weight("700") == "700"


def test_normalize_weight_falls_back_to_normal():
    assert _normalize_weight("") == "normal"
    assert _normalize_weight("ultrabold") == "normal"  # not a recognized CSS keyword and not numeric


def test_normalize_style_validates_against_known_styles():
    assert _normalize_style("italic") == "italic"
    assert _normalize_style("Oblique") == "oblique"
    assert _normalize_style("handwriting") == "normal"
    assert _normalize_style("") == "normal"


def test_resolve_font_finds_bundled_family_directly():
    resolved = resolve_font(_BUNDLED_FAMILY)
    assert resolved.matched_family == _BUNDLED_FAMILY
    assert Path(resolved.path).is_file()


def test_resolve_font_falls_through_comma_separated_list_to_first_available():
    """The first family in the CSS-style list doesn't exist anywhere, so
    resolution must skip it and match the second, bundled one — not error
    out or silently match something unrelated."""
    resolved = resolve_font(f"Definitely Not A Real Font XYZQ123, {_BUNDLED_FAMILY}")
    assert resolved.matched_family == _BUNDLED_FAMILY
    assert Path(resolved.path).is_file()


def test_resolve_font_with_no_match_anywhere_still_returns_a_usable_font():
    """None of the requested families exist — resolve_font must still
    return *some* real, on-disk font (matplotlib's own default) rather than
    raising, since a missing font shouldn't be a hard failure for the
    pipeline."""
    resolved = resolve_font("Definitely Not A Real Font XYZQ123")
    assert isinstance(resolved, ResolvedFont)
    assert Path(resolved.path).is_file()


def test_resolve_font_is_cached_by_arguments():
    a = resolve_font(_BUNDLED_FAMILY, "normal", "normal")
    b = resolve_font(_BUNDLED_FAMILY, "normal", "normal")
    assert a is b


def test_resolve_fallback_font_returns_none_when_no_font_covers_the_codepoint():
    """The very last valid Unicode codepoint (a private-use-area code point)
    is not mapped by any real font's cmap — this must come back None rather
    than crash or silently point at an unrelated glyph."""
    assert resolve_fallback_font(0x10FFFD) is None
