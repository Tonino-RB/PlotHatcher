from pathlib import Path

import pytest

from fonthatch.core.shaping import shape_block
from fonthatch.core.singleline import _LIGATURES, hershey_lines_for_glyph
from fonthatch.core.svg_text import extract_text_blocks

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def shaped_ligature_run():
    blocks = extract_text_blocks(str(FIXTURES / "ligature.svg"))
    return shape_block(blocks[0]).runs[0]


@pytest.mark.parametrize("ligature", list(_LIGATURES))
def test_ligature_produces_strokes_for_every_letter(shaped_ligature_run, ligature):
    """A precomposed ligature character (e.g. U+FB01 'fi', as some export
    tools like Affinity Designer substitute in) is one HarfBuzz glyph/one
    cluster, but must still draw every one of its letters — not silently
    vanish (ord(char) - 32 lands far outside the Hershey table) and not
    collapse to just one letter's strokes."""
    text = shaped_ligature_run.run.text
    for glyph in shaped_ligature_run.glyphs:
        if text[glyph.cluster] != ligature:
            continue
        lines = hershey_lines_for_glyph(shaped_ligature_run, glyph, "futural")
        assert lines, f"expected strokes for ligature {ligature!r}"

        letters = _LIGATURES[ligature]
        # Each constituent letter must contribute its own ink: the merged
        # glyph's x-extent must span noticeably more than a single letter,
        # not just re-draw one of them.
        xs = [p.real for line in lines for p in line]
        width = max(xs) - min(xs)
        assert width > 5.0, f"ligature {ligature!r} ({letters}) looks too narrow for {len(letters)} letters"
        return
    pytest.fail(f"ligature {ligature!r} not found in fixture run")


def test_ligature_baseline_matches_original_pen_y(shaped_ligature_run):
    text = shaped_ligature_run.run.text
    for glyph in shaped_ligature_run.glyphs:
        char = text[glyph.cluster]
        if char not in _LIGATURES:
            continue
        lines = hershey_lines_for_glyph(shaped_ligature_run, glyph, "futural")
        ys = [p.imag for line in lines for p in line]
        assert max(ys) == pytest.approx(50.0), f"{char!r} baseline should sit at y=50, got {max(ys)}"


def test_ligature_letters_advance_left_to_right(shaped_ligature_run):
    """Within one ligature glyph, later letters must sit to the right of
    earlier ones (e.g. 'fi' draws 'f' then 'i', not both stacked at x=0)."""
    text = shaped_ligature_run.run.text
    for glyph in shaped_ligature_run.glyphs:
        if text[glyph.cluster] != "ﬃ":  # "ffi" -- three letters, most telling
            continue
        lines = hershey_lines_for_glyph(shaped_ligature_run, glyph, "futural")
        xs = [p.real for line in lines for p in line]
        assert max(xs) - min(xs) > 12.0, "ffi's three letters should span noticeably more than one letter's width"
        return
    pytest.fail("ﬃ not found in fixture run")


def test_non_ligature_glyph_unaffected():
    """A char not in _LIGATURES must map to itself (dict.get default) —
    this is a regression check on the refactor that introduced ligature
    expansion, not just the new behavior."""
    blocks = extract_text_blocks(str(Path(__file__).parent / "fixtures" / "mixed.svg"))
    shaped = shape_block(blocks[0]).runs[0]
    for glyph in shaped.glyphs:
        char = shaped.run.text[glyph.cluster]
        if char == " ":
            continue
        lines = hershey_lines_for_glyph(shaped, glyph, "futural")
        assert lines, f"expected strokes for {char!r}"
