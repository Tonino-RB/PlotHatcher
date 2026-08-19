from pathlib import Path

import pytest
import vpype

import fonthatch.gui.sketch as sketch_module
from fonthatch.core.render import RenderParams
from fonthatch.gui.sketch import FontHatchSketch

FIXTURES = Path(__file__).parent / "fixtures"


def _reset_params():
    FontHatchSketch.input_path.set_value(str(FIXTURES / "mixed.svg"))
    FontHatchSketch.output_path.set_value("")
    FontHatchSketch.selected_layer.set_value(0)
    FontHatchSketch.mode.set_value("hatch")
    FontHatchSketch.fill_type.set_value("spiraling")
    FontHatchSketch.spacing.set_value(1.0)
    FontHatchSketch.inset.set_value(0.0)
    FontHatchSketch.angle.set_value(45.0)
    FontHatchSketch.pen_width.set_value(0.35)
    FontHatchSketch.merge_ends.set_value(True)
    FontHatchSketch.merge_tolerance.set_value(0.0)
    FontHatchSketch.zigzag_passes.set_value(1)
    FontHatchSketch.draw_contour.set_value(True)
    FontHatchSketch.draw_hatch.set_value(True)
    FontHatchSketch.contour_separate_layer.set_value(False)
    FontHatchSketch.contour_mode.set_value("outer")
    FontHatchSketch.singleline_font.set_value("futural*")
    FontHatchSketch.singleline_round_corners.set_value(False)
    # Selection-target override state lives at module scope (SketchClass
    # instances are recreated on every execute()/redraw, so it can't live
    # on self) — reset it directly so one test's overrides can't leak into
    # the next.
    sketch_module._default_render_params = RenderParams()
    sketch_module._layer_overrides.clear()
    sketch_module._last_seen_render_params = None
    # Matching reset for the "what's actually displayed" snapshot (see
    # `sketch_module._preview_has_content`/`_displayed_default_render_params`)
    # so one test's committed preview can't leak into the next. Tests that
    # need to inspect genuinely computed hatch/contour content (not just
    # structural things like layer names or the export path, which always
    # compute for real — see `_compute`) must go through `_compute()`.
    sketch_module._displayed_default_render_params = RenderParams()
    sketch_module._displayed_layer_overrides.clear()
    sketch_module._preview_has_content = False
    sketch_module._preview_dirty = False
    sketch_module._compute_requested = False


def _compute():
    """Simulates the sidebar's "Compute Preview" button for a headless test:
    one redraw to let `_sync_selection` settle whatever was just edited,
    then a second, forced redraw — with `_compute_requested` set, same as
    `FontHatchSketch.request_compute()` does — that actually renders real
    hatch/contour content and commits it as the displayed preview (see
    `sketch_module._commit_displayed_params`). Returns that second redraw's
    sketch instance."""
    FontHatchSketch.execute(finalize=True)
    sketch_module._compute_requested = True
    return FontHatchSketch.execute(finalize=True)


def _write_two_texts_svg(path: Path) -> None:
    path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">
  <text x="10" y="30" font-size="20">AA</text>
  <text x="10" y="80" font-size="20">BB</text>
</svg>"""
    )


def _write_two_layers_svg(path: Path) -> None:
    path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">
  <g inkscape:label="1" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">
    <text x="10" y="30" font-size="20">AA</text>
  </g>
  <g inkscape:label="2" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape">
    <text x="10" y="80" font-size="20">BB</text>
  </g>
</svg>"""
    )


def _exported_hatched_group(svg_text: str, layer_id: int = 1):
    from lxml import etree

    root = etree.fromstring(svg_text.encode("utf-8"))
    return next(el for el in root.iter() if el.get("id") == f"fonthatch-hatched-{layer_id}")


def _exported_hatched_segment_count(svg_text: str, layer_id: int = 1) -> int:
    """Matches vpype's own LineCollection.segment_count() semantics (total
    segments, i.e. points-1, across all lines) so it's directly comparable
    against the live-preview counts `_hatched_segment_count` reads off
    vpype's Document — vpype's writer emits a 2-point line as `<line>`, a
    closed line (first point == last) as `<polygon points="...">` with the
    duplicate closing point dropped (so segments == point count, the closing
    edge being implicit), and anything else as `<polyline points="...">`
    (segments == point count - 1)."""
    total = 0
    for el in _exported_hatched_group(svg_text, layer_id):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "line":
            total += 1
        elif tag == "polyline":
            total += max(0, len(el.get("points", "").split()) - 1)
        elif tag == "polygon":
            total += len(el.get("points", "").split())
    return total


def _hatched_segment_count(sketch) -> int:
    """Summed across every vpype layer named "hatched", not just the first
    match: since the live preview now gives each source layer its own
    "hatched"/"contour" pair (see `layers.add_text_hatched_layers_per_source_layer`)
    rather than merging every source layer's geometry into one shared pair,
    a multi-layer file has more than one "hatched"-named layer in
    `sketch.vsk.document`."""
    total = 0
    found = False
    for lc in sketch.vsk.document.layers.values():
        if lc.property(vpype.METADATA_FIELD_NAME) == "hatched":
            total += lc.segment_count()
            found = True
    if not found:
        raise AssertionError("no hatched layer found")
    return total


def test_preview_always_shows_both_layers():
    """Both "text" and "hatched" must always be in vsk.document, matching
    what the library/CLI produce and what actually gets saved — the viewer's
    own native per-layer visibility toggle (not a custom param here) is how
    a user declutters the preview, so it must not diverge from saved output."""
    _reset_params()
    sketch = FontHatchSketch.execute(finalize=True)
    names = {lc.property(vpype.METADATA_FIELD_NAME) for lc in sketch.vsk.document.layers.values()}
    assert "text" in names and "hatched" in names


def test_contour_mode_defaults_to_outer():
    _reset_params()
    assert FontHatchSketch.contour_mode.value == "outer"


def test_live_preview_redraw_writes_nothing_to_disk(tmp_path):
    """Regression test: draw() must never write to disk on its own — only an
    explicit Export (sidebar button / 's' shortcut) should touch the
    filesystem."""
    _reset_params()
    out_path = tmp_path / "should_not_exist.svg"
    FontHatchSketch.output_path.set_value(str(out_path))
    FontHatchSketch.execute(finalize=True)
    assert not out_path.exists()


def test_export_hides_original_text_and_writes_visible_hatched_layer(tmp_path):
    """Exercises the same export_full_document() + _write_output() the
    sidebar's "Export" button (SketchViewer.on_like, patched in
    _patch_gui_chrome) calls directly."""
    _reset_params()
    out_path = tmp_path / "out.svg"
    FontHatchSketch.output_path.set_value(str(out_path))
    sketch = FontHatchSketch.execute(finalize=True)
    svg_text = sketch.export_full_document()
    sketch._write_output(svg_text)

    assert out_path.exists()
    written = out_path.read_text()
    assert list(_exported_hatched_group(written))
    assert "display:none" in written  # the original <text>, hidden in place


def test_export_output_has_no_fill_and_stroke_width_set(tmp_path):
    """Every layer the export writes must render as strokes only (no solid
    fill) at the configured pen width — vpype's own writer sets `fill:none`
    on every layer group unconditionally, and layers.py sets each drawn
    layer's stroke-width from the configured pen_width; this just pins that
    behavior down for the GUI's own export path."""
    _reset_params()
    FontHatchSketch.pen_width.set_value(0.42)
    out_path = tmp_path / "out.svg"
    FontHatchSketch.output_path.set_value(str(out_path))
    sketch = FontHatchSketch.execute(finalize=True)
    svg_text = sketch.export_full_document()
    sketch._write_output(svg_text)

    hatched = _exported_hatched_group(out_path.read_text())
    assert hatched.get("fill") == "none"
    # pen_width is a "mm"-unit Param: the raw 0.42 typed in the UI is stored
    # converted to px (vpype's native document unit) by the time it lands in
    # HatchParams.pen_width / the layer's stroke-width metadata.
    expected_px = 0.42 * vpype.convert_length("mm")
    assert float(hatched.get("stroke-width")) == pytest.approx(expected_px)


def test_export_refuses_to_overwrite_input_file(tmp_path):
    src = tmp_path / "mine.svg"
    src.write_text((FIXTURES / "mixed.svg").read_text())
    original_contents = src.read_text()

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))
    FontHatchSketch.output_path.set_value(str(src))
    sketch = FontHatchSketch.execute(finalize=True)
    svg_text = sketch.export_full_document()
    sketch._write_output(svg_text)

    assert src.read_text() == original_contents


def test_selecting_layer_only_changes_that_layers_settings(tmp_path):
    """Selecting a layer and editing a setting must only affect that
    layer's glyphs — the other text stays on the shared default (both
    texts here are ungrouped, so both share layer_index 1) — and merely
    switching `selected_layer` (without touching a setting) must not
    record anything against the newly-selected layer."""
    from fonthatch.core.layers import build_document
    from fonthatch.core.pipeline import extract_glyph_outlines

    src = tmp_path / "two_texts.svg"
    _write_two_texts_svg(src)

    def isolated_segment_count(params: RenderParams) -> int:
        """Reference count computed straight from the library, independent
        of the GUI/override plumbing."""
        outlines = extract_glyph_outlines(str(src))
        doc, _, hatched_id, _ = build_document(str(src), outlines, params)
        return doc.layers[hatched_id].segment_count()

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))
    default_params = FontHatchSketch()._render_params()
    baseline = _hatched_segment_count(_compute())
    assert baseline == isolated_segment_count(default_params)

    # Just looking at layer #1 (no setting touched) must change nothing.
    FontHatchSketch.selected_layer.set_value(1)
    unchanged = _hatched_segment_count(FontHatchSketch.execute(finalize=True))
    assert unchanged == baseline

    # Now actually edit settings while layer #1 is selected.
    FontHatchSketch.fill_type.set_value("zigzag")
    FontHatchSketch.zigzag_passes.set_value(2)
    zigzag_params = FontHatchSketch()._render_params()
    overridden = _hatched_segment_count(_compute())
    assert overridden == isolated_segment_count(zigzag_params)
    assert overridden != baseline

    # Switching back to "all" (0) shows the same override, since both
    # texts live in layer 1.
    FontHatchSketch.selected_layer.set_value(0)
    sketch = FontHatchSketch.execute(finalize=True)
    assert _hatched_segment_count(sketch) == overridden

    # Export must reflect the same per-layer split as the live preview.
    svg_text = sketch.export_full_document()
    assert _exported_hatched_segment_count(svg_text) == overridden


def test_live_preview_pen_width_is_independent_per_layer(tmp_path):
    """Regression test: the live preview's rendered stroke thickness (what
    vpype_viewer's PREVIEW mode draws, driven by each vpype layer's own
    METADATA_FIELD_PEN_WIDTH property) must reflect each source layer's own
    configured pen_width independently — giving layer #1 its own override
    must not touch what layer #2 (never selected) renders at, even though
    both used to share one merged "hatched" vpype layer with only one pen
    width property between them."""
    src = tmp_path / "two_layers.svg"
    _write_two_layers_svg(src)

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))
    FontHatchSketch.execute(finalize=True)  # settle the shared default before overriding layer #1
    default_pen_width_px = sketch_module._default_render_params.hatch.pen_width

    FontHatchSketch.selected_layer.set_value(1)
    FontHatchSketch.pen_width.set_value(0.9)
    sketch = FontHatchSketch.execute(finalize=True)

    pen_widths = [
        lc.property(vpype.METADATA_FIELD_PEN_WIDTH)
        for lc in sketch.vsk.document.layers.values()
        if lc.property(vpype.METADATA_FIELD_NAME) == "hatched"
    ]
    assert len(pen_widths) == 2
    overridden_px = 0.9 * vpype.convert_length("mm")
    assert sorted(pen_widths) == pytest.approx(sorted([overridden_px, default_pen_width_px]))


def test_switching_selection_shows_that_targets_own_stored_settings(tmp_path):
    """The core "push back" behavior: selecting a layer that already has
    its own recorded settings must make the settings fields reflect those
    — not whatever was last typed for some other selection. Reads
    `FontHatchSketch.spacing.value` directly (the raw mm-unit storage, per
    `_set_param`) rather than through an instance, to sidestep the
    unit-factor conversion entirely."""
    src = tmp_path / "two_layers.svg"
    _write_two_layers_svg(src)

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))

    FontHatchSketch.selected_layer.set_value(1)
    FontHatchSketch.spacing.set_value(2.0)
    FontHatchSketch.execute(finalize=True)  # records layer #1's spacing=2.0

    FontHatchSketch.selected_layer.set_value(0)
    FontHatchSketch.spacing.set_value(5.0)
    FontHatchSketch.execute(finalize=True)  # records the shared default's spacing=5.0

    # Switching back to layer #1 (without touching spacing) must show its
    # own recorded value again, not the default's leftover 5.0.
    FontHatchSketch.selected_layer.set_value(1)
    FontHatchSketch.execute(finalize=True)
    assert FontHatchSketch.spacing.value == pytest.approx(2.0)

    # The shared default (layer #2, which never got its own override) must
    # still be showing 5.0 when switched back to.
    FontHatchSketch.selected_layer.set_value(0)
    FontHatchSketch.execute(finalize=True)
    assert FontHatchSketch.spacing.value == pytest.approx(5.0)


def test_reset_selected_layer_clears_override_and_deselects(tmp_path):
    """Calling reset_selected_layer() while a layer is selected — what the
    sidebar's "Reset to Default" button does (SketchViewer.on_reset,
    patched in _patch_gui_chrome) — must drop that layer's own override
    and snap selected_layer back to 0 ("all"), leaving the layer
    indistinguishable from one that was never singled out in the first
    place."""
    src = tmp_path / "two_layers.svg"
    _write_two_layers_svg(src)

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))
    default_params = FontHatchSketch()._render_params()
    baseline = _hatched_segment_count(_compute())

    # Give layer #1 its own override.
    FontHatchSketch.selected_layer.set_value(1)
    FontHatchSketch.fill_type.set_value("zigzag")
    FontHatchSketch.zigzag_passes.set_value(2)
    overridden = _hatched_segment_count(_compute())
    assert overridden != baseline

    # Reset it. Dropping the override is itself a settings change, so the
    # live preview keeps showing the overridden result (see
    # `sketch_module._preview_has_content`) until explicitly recomputed —
    # same as any other edit.
    sketch = FontHatchSketch.execute(finalize=True)
    sketch.reset_selected_layer()
    assert FontHatchSketch.selected_layer.value == 0
    reset_sketch = _compute()
    assert _hatched_segment_count(reset_sketch) == baseline

    # The override is really gone, not just hidden: selecting layer #1
    # again shows the shared default.
    FontHatchSketch.selected_layer.set_value(1)
    sketch = FontHatchSketch.execute(finalize=True)
    assert FontHatchSketch.fill_type.value == default_params.hatch.fill_type.value
    assert FontHatchSketch.zigzag_passes.value == default_params.hatch.zigzag_passes
    assert _hatched_segment_count(sketch) == baseline


def test_reset_selected_layer_is_a_noop_with_nothing_selected(tmp_path):
    """Calling reset_selected_layer() while selected_layer is already 0
    has nothing to drop — it must just quietly do nothing, not error or
    otherwise change anything."""
    src = tmp_path / "two_layers.svg"
    _write_two_layers_svg(src)

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))
    sketch = FontHatchSketch.execute(finalize=True)

    sketch.reset_selected_layer()
    assert FontHatchSketch.selected_layer.value == 0


def test_selection_highlight_layer_only_when_something_selected(tmp_path):
    """The live preview marks which layer is currently selected with a
    "selection" layer, but only while selected_layer is nonzero — with
    nothing selected there's no single set of glyphs to box, so no such
    layer should appear. It must also never leak into
    export_full_document's output."""
    src = tmp_path / "two_layers.svg"
    _write_two_layers_svg(src)

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))

    sketch = FontHatchSketch.execute(finalize=True)
    names = {lc.property(vpype.METADATA_FIELD_NAME) for lc in sketch.vsk.document.layers.values()}
    assert sketch_module.SELECTION_LAYER_NAME not in names

    FontHatchSketch.selected_layer.set_value(1)
    sketch = FontHatchSketch.execute(finalize=True)
    names = {lc.property(vpype.METADATA_FIELD_NAME) for lc in sketch.vsk.document.layers.values()}
    assert sketch_module.SELECTION_LAYER_NAME in names

    svg_text = sketch.export_full_document()
    assert sketch_module.SELECTION_LAYER_NAME not in svg_text


def test_post_finalize_hides_selection_layer_too(tmp_path):
    """vsketch's own native save flow reuses vsk.document (unlike our
    Export button), so the selection-highlight layer does end up in the
    file it writes — post_finalize must hide it, same as "text"."""
    src = tmp_path / "two_layers.svg"
    _write_two_layers_svg(src)

    _reset_params()
    FontHatchSketch.input_path.set_value(str(src))
    FontHatchSketch.selected_layer.set_value(1)
    sketch = FontHatchSketch.execute(finalize=True)

    saved_path = tmp_path / "native_save.svg"
    with open(saved_path, "w") as fp:
        vpype.write_svg(fp, sketch.vsk.document)
    assert f'inkscape:label="{sketch_module.SELECTION_LAYER_NAME}"' in saved_path.read_text()

    sketch.post_finalize(sketch.vsk, saved_path)
    svg_text = saved_path.read_text()
    label_index = svg_text.index(f'inkscape:label="{sketch_module.SELECTION_LAYER_NAME}"')
    group_start = svg_text.rindex("<g", 0, label_index)
    group_end = svg_text.index(">", label_index)
    assert "display:none" in svg_text[group_start:group_end]


def test_mode_switch_changes_hatched_layer_geometry():
    """Regression test for the reported bug where toggling mode appeared to
    have no effect: hatch vs singleline must produce different geometry in
    the "hatched" layer for the same input/params. Both measurements go
    through `_compute()`: switching mode is itself a settings change, so the
    live preview keeps showing the *previous* mode's result until explicitly
    recomputed — see `test_live_preview_withholds_fill_until_computed`."""
    _reset_params()
    FontHatchSketch.mode.set_value("hatch")
    hatch_count = _hatched_segment_count(_compute())

    FontHatchSketch.mode.set_value("singleline")
    singleline_count = _hatched_segment_count(_compute())

    assert hatch_count != singleline_count


def test_draw_hatch_false_omits_fill_from_hatched_layer():
    """draw_hatch, like draw_contour, gates HATCH mode content independently
    — turning it off must still leave the contour (draw_contour defaults
    True), just without the fill strokes."""
    _reset_params()
    with_fill = _hatched_segment_count(_compute())

    FontHatchSketch.draw_hatch.set_value(False)
    contour_only = _hatched_segment_count(_compute())
    assert contour_only < with_fill
    assert contour_only > 0  # the contour itself is still drawn


def test_draw_hatch_and_draw_contour_both_false_is_empty():
    _reset_params()
    FontHatchSketch.draw_hatch.set_value(False)
    FontHatchSketch.draw_contour.set_value(False)
    assert _hatched_segment_count(_compute()) == 0


def test_singleline_mode_headless():
    _reset_params()
    FontHatchSketch.mode.set_value("singleline")
    FontHatchSketch.singleline_font.set_value("timesr*")
    FontHatchSketch.singleline_round_corners.set_value(True)
    sketch = FontHatchSketch.execute(finalize=True)
    assert not sketch.vsk.document.is_empty()


def test_zigzag_fill_type_headless():
    _reset_params()
    FontHatchSketch.fill_type.set_value("zigzag")
    FontHatchSketch.zigzag_passes.set_value(2)
    sketch = FontHatchSketch.execute(finalize=True)
    assert not sketch.vsk.document.is_empty()


def test_merge_tolerance_param_headless():
    _reset_params()
    FontHatchSketch.merge_tolerance.set_value(0.5)
    sketch = FontHatchSketch.execute(finalize=True)
    assert not sketch.vsk.document.is_empty()


def test_live_preview_withholds_fill_until_computed():
    """Opening a file must show only the glyphs' raw outlines — no contour,
    no hatch fill — until "Compute Preview" is pressed, so opening a file
    never pays for the expensive hatch computation unless the user actually
    asks to see it."""
    _reset_params()
    assert _hatched_segment_count(FontHatchSketch.execute(finalize=True)) == 0

    computed = _hatched_segment_count(_compute())
    assert computed > 0

    # Editing a setting afterward must keep showing the last computed
    # result on screen — not reset back to the raw-outline preview — until
    # Compute Preview is pressed again.
    FontHatchSketch.spacing.set_value(2.0)
    assert _hatched_segment_count(FontHatchSketch.execute(finalize=True)) == computed

    assert _hatched_segment_count(_compute()) > 0


def test_missing_input_file_does_not_crash():
    _reset_params()
    FontHatchSketch.input_path.set_value(str(FIXTURES / "does_not_exist.svg"))
    sketch = FontHatchSketch.execute(finalize=True)
    assert sketch is not None


def test_post_finalize_hides_text_layer_after_native_save(tmp_path):
    """Simulates vsketch's own interactive-save flow: write via vpype's
    plain writer (as the viewer's native save does), then call
    post_finalize — mirrors what `vsketch_cli`'s SketchViewer wires up."""
    _reset_params()
    sketch = FontHatchSketch.execute(finalize=True)

    saved_path = tmp_path / "native_save.svg"
    with open(saved_path, "w") as fp:
        vpype.write_svg(fp, sketch.vsk.document)
    assert "display:none" not in saved_path.read_text()

    sketch.post_finalize(sketch.vsk, saved_path)
    svg_text = saved_path.read_text()
    assert "display:none" in svg_text
