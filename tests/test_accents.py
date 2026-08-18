from pathlib import Path

import pytest

from fonthatch.core.accents import _PATCHED_FONT_NAMES
from fonthatch.core.shaping import shape_block
from fonthatch.core.singleline import hershey_lines_for_glyph
from fonthatch.core.svg_text import extract_text_blocks

FIXTURES = Path(__file__).parent / "fixtures"
ACCENTED_CHARS = "àèéëùçÀÈÉËÙÇ"
PLAIN_BASE_LETTERS = "aeucAEUC"


@pytest.fixture(scope="module")
def shaped_accented_run():
    svg = FIXTURES / "accented.svg"
    blocks = extract_text_blocks(str(svg))
    return shape_block(blocks[0]).runs[0]


@pytest.mark.parametrize("font_name", _PATCHED_FONT_NAMES)
def test_accented_glyphs_produce_strokes(shaped_accented_run, font_name):
    for glyph in shaped_accented_run.glyphs:
        char = shaped_accented_run.run.text[glyph.cluster]
        lines = hershey_lines_for_glyph(shaped_accented_run, glyph, font_name)
        assert lines, f"expected strokes for {char!r} in {font_name!r}"


@pytest.mark.parametrize("font_name", _PATCHED_FONT_NAMES)
def test_accented_glyph_baseline_matches_original_pen_y(shaped_accented_run, font_name):
    """Same invariant as test_singleline's baseline test: the substitute's
    lowest point must land on the source text's baseline y (50), just like
    any other Hershey substitute — the diacritic sits above the letter (or,
    for cedilla, below the baseline), so it must never push the letter
    itself off its baseline."""
    for glyph in shaped_accented_run.glyphs:
        char = shaped_accented_run.run.text[glyph.cluster]
        if char not in PLAIN_BASE_LETTERS:
            continue
        lines = hershey_lines_for_glyph(shaped_accented_run, glyph, font_name)
        ys = [p.imag for line in lines for p in line]
        assert max(ys) == pytest.approx(50.0), f"{char!r} baseline should sit at y=50, got {max(ys)}"


@pytest.mark.parametrize("char", list(ACCENTED_CHARS))
def test_accented_mark_sits_above_or_below_letter_ink(shaped_accented_run, char):
    """The diacritic must extend outside the base letter's own ink: above
    for grave/acute/diaeresis, below the baseline for the cedilla —
    otherwise it would be indistinguishable from the plain letter."""
    font_name = "futural"
    for glyph in shaped_accented_run.glyphs:
        if shaped_accented_run.run.text[glyph.cluster] != char:
            continue
        lines = hershey_lines_for_glyph(shaped_accented_run, glyph, font_name)
        ys = [p.imag for line in lines for p in line]
        if char in "çÇ":
            assert max(ys) > 50.0 + 0.5, "cedilla should hang below the baseline"
        else:
            assert min(ys) < 50.0 - 8.0, "diacritic should sit clearly above the letter"
        return
    pytest.fail(f"{char!r} not found in fixture run")


def test_diaeresis_is_two_separate_dots(shaped_accented_run):
    """ë/Ë must render as two distinct dots (not a single merged stroke) —
    otherwise it's indistinguishable from a grave/acute accent."""
    for accented_char in ("ë", "Ë"):
        for glyph in shaped_accented_run.glyphs:
            if shaped_accented_run.run.text[glyph.cluster] != accented_char:
                continue
            base_char = "e" if accented_char == "ë" else "E"
            base_glyph = next(
                g for g in shaped_accented_run.glyphs if shaped_accented_run.run.text[g.cluster] == base_char
            )
            base_lines = hershey_lines_for_glyph(shaped_accented_run, base_glyph, "futural")
            lines = hershey_lines_for_glyph(shaped_accented_run, glyph, "futural")
            assert len(lines) == len(base_lines) + 2, f"expected exactly two extra dot strokes for {accented_char!r}"
            break


def test_unpatched_font_still_returns_nothing_for_accents():
    """Fonts outside the patched set keep today's behavior (no glyph, no
    crash) — this is a targeted fix for a handful of fonts, not every font
    vpype bundles."""
    blocks = extract_text_blocks(str(FIXTURES / "accented.svg"))
    shaped = shape_block(blocks[0]).runs[0]
    for glyph in shaped.glyphs:
        char = shaped.run.text[glyph.cluster]
        if char in ACCENTED_CHARS:
            assert hershey_lines_for_glyph(shaped, glyph, "scriptc") == []
