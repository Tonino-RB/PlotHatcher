from pathlib import Path

from fonthatch.core.svg_text import extract_text_blocks

FIXTURES = Path(__file__).parent / "fixtures"


def test_text_before_nested_tspan_stays_in_document_order():
    """Regression test: svgelements' flat element iterator yields a nested
    tspan's content *before* its containing element's own direct text
    (verified directly against the installed version — the reverse of
    document order), which used to corrupt run ordering/positions for any
    SVG with nested tspans. Structure/order must come from lxml, not that
    iterator."""
    blocks = extract_text_blocks(str(FIXTURES / "nested_transform.svg"))
    assert len(blocks) == 1
    texts = [r.text for r in blocks[0].runs]
    assert texts == ["Ab", "C"]


def test_position_on_wrapper_tspan_reaches_nested_text():
    """Regression test for a real-world Inkscape pattern (text flowed into
    a shape produces `<tspan x=".." y=".."><tspan style="...">text</tspan>
    </tspan>`): a tspan with x/y but no direct text of its own must still
    have that position land on the first real character found anywhere in
    its subtree. This previously left the position stranded on the empty
    wrapper, so the actual text fell back to a (0, 0) origin and, combined
    with the element's own transform, ended up rendered off-canvas."""
    blocks = extract_text_blocks(str(FIXTURES / "nested_tspan_position.svg"))
    assert len(blocks) == 1
    run = blocks[0].runs[0]
    assert run.text == "Nested"
    assert run.x == 10.0
    assert run.y == 50.0


def test_block_index_is_1_based_and_skips_discarded_empty_text(tmp_path):
    """block_index numbers only the <text> elements that actually survive
    (a <text> with no real content anywhere in it is discarded, not
    numbered), so it must stay gap-free over kept blocks and consistent
    for every run within the same block."""
    svg = tmp_path / "three_texts.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">
  <text x="0" y="10">First<tspan>Run</tspan></text>
  <text x="0" y="20">   </text>
  <text x="0" y="30">Third</text>
</svg>"""
    )
    blocks = extract_text_blocks(str(svg))
    assert len(blocks) == 2
    assert {r.block_index for r in blocks[0].runs} == {1}
    assert {r.block_index for r in blocks[1].runs} == {2}


def test_percentage_width_height_does_not_scale_document(tmp_path):
    """Regression test: some exporters (e.g. Affinity Designer) emit
    width="100%" height="100%" on the root <svg>, relying on viewBox alone
    for intrinsic size. vpype.convert_length rejects "100%" (no containing
    block to resolve it against), and the old fallback then regex-parsed the
    leading digits out of "100%" as if it were a bare "100" — i.e. 100 user
    units wide, not the actual viewBox width. Every glyph position and
    font-size then got scaled down by whatever tiny factor 100 / viewBox
    width worked out to, producing near-invisible text. A percentage must be
    treated as unspecified (falls back to the viewBox dimension) instead."""
    svg = tmp_path / "percent_size.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 2575 4961">
  <text x="10" y="20" style="font-size:100px;">Big</text>
</svg>"""
    )
    blocks = extract_text_blocks(str(svg))
    assert len(blocks) == 1
    run = blocks[0].runs[0]
    assert run.font_size == 100.0
    from svgelements import Point

    p = Point(run.x, run.y) * run.transform
    assert (round(p.x, 3), round(p.y, 3)) == (10.0, 20.0)


def test_layer_index_matches_vpype_own_numbering(tmp_path):
    """layer_index must agree with vpype.read_multilayer_svg's own layer
    numbering for the same file (verified directly against it below) —
    that's the numbering the GUI's `selected_layer` param and the vpype
    viewer's native "Layer" toggle both refer to, so a mismatch would make
    "layer 2" mean two different things in the same app."""
    import vpype

    svg = tmp_path / "layers.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="200" height="200" viewBox="0 0 200 200">
  <g inkscape:groupmode="layer" inkscape:label="Layer 5">
    <rect x="0" y="0" width="5" height="5"/>
    <text x="0" y="10">Labeled</text>
  </g>
  <g inkscape:groupmode="layer" id="layerNoLabel">
    <rect x="0" y="0" width="5" height="5"/>
    <text x="0" y="20">Fallback</text>
  </g>
  <rect x="1" y="1" width="2" height="2"/>
  <text x="0" y="30">Ungrouped</text>
</svg>"""
    )
    # vpype's own reader ignores <text> entirely, so each group also carries
    # a <rect> — otherwise a text-only group produces an empty layer that
    # vpype never materializes in `doc.layers`, leaving nothing to compare
    # against.
    doc = vpype.read_multilayer_svg(str(svg), quantization=0.1)
    assert sorted(doc.layers.keys()) == [1, 2, 5]

    blocks = extract_text_blocks(str(svg))
    by_text = {block.runs[0].text: block.runs[0].layer_index for block in blocks}
    assert by_text == {"Labeled": 5, "Fallback": 2, "Ungrouped": 1}


def test_own_transform_not_applied_twice():
    """Regression test: the <text> element's own transform must be applied
    exactly once (it was briefly getting composed with itself via a
    double-application bug introduced while fixing the ordering issue)."""
    blocks = extract_text_blocks(str(FIXTURES / "nested_tspan_position.svg"))
    transform = blocks[0].runs[0].transform
    assert transform.a == 1.0
    assert transform.e == 50.0
