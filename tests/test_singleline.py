from pathlib import Path

import pytest

from fonthatch.core.shaping import shape_block
from fonthatch.core.singleline import hershey_lines_for_glyph
from fonthatch.core.svg_text import extract_text_blocks

FIXTURES = Path(__file__).parent / "fixtures"


def test_hershey_substitute_baseline_matches_original_pen_y():
    """The substitute glyph's baseline must land exactly at the original
    text's baseline y (50, from the fixture's text y="50") — this is what
    'keep the same overall layout' hinges on, distinct from proportions
    (cap-height/width), which are allowed to differ between typefaces."""
    blocks = extract_text_blocks(str(FIXTURES / "mixed.svg"))
    shaped = shape_block(blocks[0])
    for run in shaped.runs:
        for glyph in run.glyphs:
            char = run.run.text[glyph.cluster]
            if char == " ":
                continue
            lines = hershey_lines_for_glyph(run, glyph, "futural")
            assert lines, f"expected strokes for {char!r}"
            ys = [p.imag for line in lines for p in line]
            assert max(ys) == pytest.approx(50.0), f"{char!r} baseline should sit at y=50, got {max(ys)}"


def test_hershey_space_produces_no_strokes():
    blocks = extract_text_blocks(str(FIXTURES / "mixed.svg"))
    shaped = shape_block(blocks[0])
    for run in shaped.runs:
        for glyph in run.glyphs:
            if run.run.text[glyph.cluster] == " ":
                assert hershey_lines_for_glyph(run, glyph, "futural") == []


def test_hershey_substitute_advances_left_to_right_matching_pen_order():
    blocks = extract_text_blocks(str(FIXTURES / "mixed.svg"))
    shaped = shape_block(blocks[0])
    run = shaped.runs[0]
    prev_max_x = None
    for glyph in run.glyphs:
        char = run.run.text[glyph.cluster]
        lines = hershey_lines_for_glyph(run, glyph, "futural")
        if not lines:
            continue
        min_x = min(p.real for line in lines for p in line)
        if prev_max_x is not None:
            assert min_x >= prev_max_x - 0.5, f"{char!r} overlaps the previous glyph unexpectedly"
        prev_max_x = max(p.real for line in lines for p in line)
