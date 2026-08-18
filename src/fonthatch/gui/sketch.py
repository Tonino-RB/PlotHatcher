"""vsketch GUI: live preview + tweakable parameters for fonthatch.

Runs via `vsk run` (see the `fonthatch-gui` console script / `launch()`
below). vsketch's own ``Param`` system has no file-path widget (confirmed
against the installed version: ``Param`` only supports int/float/str/bool
with optional min/max/choices — see ``vsketch.Param.__doc__``), so
``input_path`` is a plain text field the user edits directly in the
viewer's parameter panel. It also has **no conditional enable/grey-out
mechanism** (checked the installed vsketch/vsketch_cli source directly —
there's no such API), so params only relevant to one ``mode`` can't be
disabled in the UI; the ``singleline_``-prefixed ones are the only ones
still named that way, so it's at least visually clear those are
singleline-only — the rest are hatch params and need no prefix of their
own since hatch is the default mode.

Both "text" and "hatched" layers are always added to the live preview,
matching exactly what the library/CLI produce (and what gets written on
save) — the viewer has its own native per-layer visibility toggle (the
"Layer" button in its toolbar), so use that to hide the "text" outline
layer if it makes mode/hatch changes hard to see, rather than this sketch
guessing at a default and silently diverging from what actually gets saved.

``selected_layer`` lets each of the SVG's own top-level layers get its own
hatch/mode settings instead of one shared setting for everything: 0 means
"edit the shared default that applies to any layer without an override of
its own", N means "edit only layer N" (numbered exactly the way vpype
itself numbers a document's top-level layers, see ``_toplevel_layer_ids``
in svg_text.py — so it matches the same numbers the vpype viewer's own
"Layer" toggle shows). Unlike a plain number field, it's rendered as a
dropdown listing the layers actually present in whatever file is loaded,
each entry labelled with that layer's own text (see ``_layer_choices``,
rebuilt on every redraw by the patched ``ChoiceParamWidget`` — see below
for why that rebuild is necessary at all). ``reset_selected_layer`` (a
real sidebar button now, not a self-unchecking checkbox — see
``_patch_gui_chrome``) drops whichever layer is currently selected's own
override and snaps its index back to 0, so it goes back to being covered
by the shared default, same as if it had never been singled out.

An earlier version of this also let individual ``<text>`` elements get
their own override (``selected_text``/``select_mode``), grouped by
``TextRun.block_index`` instead of ``layer_index``. That's gone: per-layer
overrides are the only selection axis now, and every layer, no matter how
many ``<text>`` elements it contains, is edited as one unit.

Three problems this runs into, all worked around below:

1. vsketch's parameter panel has no built-in way to show different values
   per selection (no dynamic choices, no programmatic push from sketch
   back into the UI). Worked around by directly writing into the
   class-level ``Param`` objects (bypassing their widgets) whenever the
   resolved target's settings differ from what's currently displayed, then
   calling ``ParamsWidget.update_from_param()`` — a real (if underused)
   vsketch_cli method, already used internally for its own config-load
   feature — via a ``redraw_sketch_completed`` patch in
   ``_patch_gui_chrome``. See ``_sync_selection``/``_push_render_params``.
2. ``Param.choices`` (what populates a dropdown) is likewise fixed at
   class-definition time — there's no API to repopulate it from whatever
   file happens to be loaded, which ``selected_layer``'s dropdown needs to
   do every time a different file (with a different set of layers) is
   opened. Worked around the same way the comma-decimal fix below does:
   subclassing ``ChoiceParamWidget`` (not editing the installed
   vsketch_cli package) so its ``update_from_param()`` — already wired to
   fire after every redraw, per problem 1 above — rebuilds its item list
   from ``_layer_choices`` instead of trusting the (stale) items built at
   construction time. See ``_patch_param_widgets``.
3. There's no way to click a layer directly in the canvas to select it
   either — picking is index-based (via the dropdown) rather than
   click-based. To at least confirm *which* layer a given number refers
   to, the live preview draws a thin magenta rectangle around its glyphs
   whenever ``selected_layer`` is nonzero (see
   ``_selection_highlight_lines``, used in ``_draw_result``). This is a
   UI-only annotation: never present in ``export_full_document``'s output
   (which never touches ``vsk.document`` at all), but *is* present in
   vsketch's own native ``vsk.document``-based save, so ``post_finalize``
   hides it by name the same way it already hides the "text" layer.

See the module-level ``_layer_overrides``/``_default_render_params`` state
below for how the recorded settings themselves are stored.

Nothing is written to disk just from live-preview redraws (an earlier
version did, on every keystroke — including while still typing a path —
which could overwrite an unrelated, or even the *input*, file with no
warning). Writing only happens via an explicit action: the sidebar's
"Export" button (repurposed from vsketch_cli's built-in "LIKE!" button —
see ``_patch_gui_chrome`` below) or its 's' keyboard shortcut, which calls
``_write_output`` directly against the currently rendered document. That
refuses to write over the resolved input file, and hides the "text" layer
in the saved SVG the same way ``post_finalize`` does for vsketch's own
native save (`vsk save`).

Also worked around: ``vsketch_cli``'s plain ``TextParamWidget`` (used for
``input_path``/``output_path``, the only string params with no
``choices``) calls ``QTextEdit.setText()`` unconditionally every time
``update_from_param()`` runs — which, per problem 1 above, is *every
single redraw*, and a plain text param with no debounce redraws on every
keystroke. ``setText()`` resets the cursor to column 0 regardless of
whether the text actually changed, which made editing partway through a
path impossible — the cursor jumped back to the start after each
character typed. ``_patch_param_widgets`` swaps in a subclass that skips
the redundant ``setText()`` whenever the redrawn value already matches
what's on screen (the overwhelmingly common case, since it's usually the
very edit that triggered the redraw), only actually touching the cursor
on a genuine external change (e.g. loading a saved config).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import vpype
import vsketch

from fonthatch.core.hatch import ContourMode, FillType, HatchParams
from fonthatch.core.layers import DEFAULT_QUANTIZATION, TEXT_LAYER_NAME, build_document
from fonthatch.core.outlines import GlyphOutline
from fonthatch.core.pipeline import extract_glyph_outlines
from fonthatch.core.render import RenderMode, RenderParams
from fonthatch.core.accents import mark_font_name, marked_font_names, unmark_font_name
from fonthatch.core.svg_output import hide_layers_in_file, write_svg

SELECTION_LAYER_NAME = "selection"
_SELECTION_HIGHLIGHT_COLOR = "magenta"
_SELECTION_HIGHLIGHT_PAD = 3.0
"""User units of breathing room around the selected glyphs' bounds, so the
highlight rectangle doesn't hug the ink exactly."""

_outline_cache: dict[tuple[str, float], list[GlyphOutline]] = {}

_render_cache: dict[tuple, tuple] = {}
"""(id(glyph), effective-params) -> that glyph's rendered strokes (see
add_text_hatched_layers). Cleared in lockstep with `_outline_cache` — its
keys are only meaningful for the GlyphOutline objects currently in that
cache, since `id()` can be reused once an old GlyphOutline is garbage
collected. Without this, every redraw (i.e. every keystroke/slider-tick)
recomputes the hatch fill for every glyph in the document, even when only
one layer's settings changed; with it, a redraw only recomputes the glyphs
whose own effective params actually differ from what's already cached."""

_LAYER_LABEL_TEXT_MAX = 40
"""Characters of a layer's own text kept in its dropdown label before
truncating with an ellipsis — long text blocks would otherwise blow out
the sidebar's width."""

_layer_choices: list[tuple[int, str]] = [(0, "0 — All layers (shared default)")]
"""(layer_index, dropdown label) pairs for whichever file was most
recently loaded (see ``_cached_glyph_outlines``) — always starts with the
"0 = shared default" entry. Read by the patched ``ChoiceParamWidget``
(``_patch_param_widgets``) every redraw to rebuild ``selected_layer``'s
dropdown, since ``vsketch.Param.choices`` is otherwise fixed at
class-definition time and can't be repopulated from whatever file happens
to be loaded."""

# Selection-target overrides: keyed by whichever layer is selected when a
# setting is changed, persisted at module scope rather than on the sketch
# instance, since SketchClass.execute() constructs a brand-new instance on
# every single redraw (confirmed against the installed vsketch source) —
# instance attributes would never survive between one draw() and the next.
_default_render_params = RenderParams()
"""Applies to any layer without an override of its own — i.e. the whole
document, for anyone who never touches selected_layer."""
_layer_overrides: dict[tuple[str, int], RenderParams] = {}
"""(input_path, layer_index) -> that layer's own RenderParams, keyed the
same way vpype itself numbers a document's top-level layers (see
svg_text._toplevel_layer_ids)."""
_last_seen_render_params: RenderParams | None = None
"""What the settings fields showed as of the previous draw(), so a redraw
triggered by *only* moving the selection (no settings actually touched, or
just an already-resolved value getting pushed back into the widgets) can
be told apart from one where a setting was genuinely edited — the former
must not clobber the newly-selected target's stored override with
whatever the fields happened to still be showing."""


def _layer_choice_label(index: int, text: str) -> str:
    if not text:
        return f"{index} — (no text)"
    if len(text) > _LAYER_LABEL_TEXT_MAX:
        text = text[: _LAYER_LABEL_TEXT_MAX - 1] + "…"
    return f"{index} — {text}"


def _cached_glyph_outlines(input_path: str) -> list[GlyphOutline] | None:
    """Shaping + outline extraction only depends on the input file, not on
    hatch params, so it's cached across redraws and only recomputed when
    the file's mtime changes."""
    global _layer_choices

    try:
        mtime = os.path.getmtime(input_path)
    except OSError:
        return None
    key = (input_path, mtime)
    if key not in _outline_cache:
        _outline_cache.clear()
        _render_cache.clear()
        outlines = extract_glyph_outlines(input_path)
        _outline_cache[key] = outlines
        layer_groups = _describe_groups(outlines, key=lambda g: g.run.layer_index)
        for index, text in layer_groups:
            print(f"fonthatch: layer #{index}: {text!r}")
        _layer_choices = [(0, "0 — All layers (shared default)")] + [
            (index, _layer_choice_label(index, text)) for index, text in layer_groups
        ]
    return _outline_cache[key]


def _describe_groups(glyph_outlines: list[GlyphOutline], key) -> list[tuple[int, str]]:
    """One (index, concatenated text) pair per distinct value of `key(glyph)`
    (here, always `layer_index`), in document order — also used to build
    the selected_layer dropdown's labels (see `_cached_glyph_outlines`),
    since vsketch's Param choices are fixed at class-definition time and
    can't be repopulated from whatever file happens to be loaded."""
    texts: dict[int, list[str]] = {}
    seen_run_ids: set[int] = set()
    for glyph in glyph_outlines:
        if id(glyph.run) in seen_run_ids:
            continue
        seen_run_ids.add(id(glyph.run))
        texts.setdefault(key(glyph), []).append(glyph.run.text)
    return sorted((index, "".join(parts)) for index, parts in texts.items())


def _current_target(selected_layer: int) -> int | None:
    """None means "no specific layer selected" -> the shared default."""
    return selected_layer if selected_layer > 0 else None


def _render_params_for_target(input_path: str, target: int | None) -> RenderParams:
    if target is None:
        return _default_render_params
    return _layer_overrides.get((input_path, target), _default_render_params)


def _reset_target_override(input_path: str, target: int | None) -> None:
    """Drops `target`'s own stored override, if it has one — used by
    `FontHatchSketch.reset_selected_layer`. Once gone,
    `_render_params_for_target` naturally falls back to
    `_default_render_params` again for it, same as a layer that was never
    singled out in the first place."""
    if target is None:
        return
    _layer_overrides.pop((input_path, target), None)


def _set_param(param: vsketch.Param, value) -> None:
    """`Param.__get__` returns `factor * value` for unit-bearing params
    (spacing/inset/pen_width/merge_tolerance all use unit="mm") — so
    `params.hatch.spacing` etc. (read the normal way, off an instance) is
    already factor-multiplied, and writing it straight into `.value` via
    `set_value` would double the factor on the next read. Divide it back
    out first; params with no unit (factor is None) are unaffected."""
    param.set_value(value / param.factor if param.factor is not None else value)


def _push_render_params(sketch_cls: type, params: RenderParams) -> None:
    """Writes `params`' fields directly into the *class*-level Param objects
    (bypassing their widgets, so nothing gets emitted/re-triggered here) —
    the actual on-screen refresh happens separately, when
    `ParamsWidget.update_from_param()` gets called (patched into
    `redraw_sketch_completed` in `_patch_gui_chrome`)."""
    _set_param(sketch_cls.mode, params.mode.value)
    _set_param(sketch_cls.fill_type, params.hatch.fill_type.value)
    _set_param(sketch_cls.spacing, params.hatch.spacing)
    _set_param(sketch_cls.fill_spacing, params.hatch.fill_spacing or 0.0)
    _set_param(sketch_cls.inset, params.hatch.inset)
    _set_param(sketch_cls.angle, params.hatch.angle)
    _set_param(sketch_cls.pen_width, params.hatch.pen_width)
    _set_param(sketch_cls.merge_ends, params.hatch.merge_ends)
    _set_param(sketch_cls.merge_tolerance, params.hatch.merge_tolerance)
    _set_param(sketch_cls.zigzag_passes, params.hatch.zigzag_passes)
    _set_param(sketch_cls.draw_contour, params.draw_contour)
    _set_param(sketch_cls.draw_hatch, params.draw_hatch)
    _set_param(sketch_cls.contour_separate_layer, params.contour_separate_layer)
    _set_param(sketch_cls.contour_mode, params.contour_mode.value)
    _set_param(sketch_cls.singleline_font, mark_font_name(params.singleline_font))
    _set_param(sketch_cls.singleline_round_corners, params.singleline_round_corners)


def _sync_selection(sketch_cls: type, input_path: str, target: int | None, current: RenderParams) -> RenderParams:
    """Called once per draw(). Records `current` against `target` if it
    actually differs from what was last displayed (a genuine edit — merely
    moving the selection re-triggers draw() without changing any setting,
    so this is a no-op then). Either way, resolves whatever `target` is
    actually storing — itself, if just edited; whatever was already there,
    if the user only just switched selection — and pushes it back into the
    Param objects so the panel always reflects the right thing for
    whatever is currently selected. Returns the resolved RenderParams."""
    global _default_render_params, _last_seen_render_params

    if current != _last_seen_render_params:
        if target is None:
            _default_render_params = current
        else:
            _layer_overrides[(input_path, target)] = current

    resolved = _render_params_for_target(input_path, target)
    _push_render_params(sketch_cls, resolved)
    _last_seen_render_params = resolved
    return resolved


def _selection_highlight_lines(glyph_outlines: list[GlyphOutline], target: int | None) -> list[np.ndarray]:
    """A single rectangle outline around whatever glyphs belong to layer
    `target`, or nothing if `target` is None (selected_layer at 0 — "all",
    which has no single set of glyphs to box)."""
    if target is None:
        return []
    polygons = [
        g.polygon
        for g in glyph_outlines
        if g.run.layer_index == target and g.polygon is not None and not g.polygon.is_empty
    ]
    if not polygons:
        return []
    minx = min(p.bounds[0] for p in polygons) - _SELECTION_HIGHLIGHT_PAD
    miny = min(p.bounds[1] for p in polygons) - _SELECTION_HIGHLIGHT_PAD
    maxx = max(p.bounds[2] for p in polygons) + _SELECTION_HIGHLIGHT_PAD
    maxy = max(p.bounds[3] for p in polygons) + _SELECTION_HIGHLIGHT_PAD
    corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    return [np.array([complex(x, y) for x, y in corners])]


class FontHatchSketch(vsketch.SketchClass):
    input_path = vsketch.Param("input.svg")
    output_path = vsketch.Param("")
    """If blank, defaults to "<input stem>_hatched.svg" next to the input file."""

    selected_layer = vsketch.Param(0, choices=[0])
    """Which of the SVG's own top-level layers is being edited: 0 edits the
    shared default that applies to any layer without an override of its
    own; N edits only layer N. Numbered the same way vpype itself numbers
    layers (see svg_text._toplevel_layer_ids), so "layer 2" here means the
    same thing as "layer 2" in the vpype viewer's native "Layer" toggle.
    Rendered as a dropdown rather than a plain number field: its list of
    choices is rebuilt on every redraw from whichever file is actually
    loaded (see `_layer_choices` and `_patch_param_widgets`), each entry
    labelled with that layer's own text so it's clear which is which
    without needing the console printout. Changing this alone changes
    nothing — only a setting changed *while* a given layer is selected
    gets recorded against that layer specifically; switch back to 0 (or
    press "Reset to Default") to edit the shared default again."""

    mode = vsketch.Param(RenderMode.HATCH.value, choices=[m.value for m in RenderMode])

    fill_type = vsketch.Param(FillType.SPIRALING.value, choices=[f.value for f in FillType])
    spacing = vsketch.Param(1.0, 0.05, 50.0, step=0.1, unit="mm", decimals=2)
    fill_spacing = vsketch.Param(0.0, 0.0, 50.0, step=0.05, unit="mm", decimals=2)
    """Line-to-line spacing for the coverage fills (spiraling/zigzag/glyph_fill).
    0 means "use the pen width" — adjacent strokes exactly tangent."""
    inset = vsketch.Param(0.0, 0.0, 50.0, step=0.05, unit="mm", decimals=2)
    angle = vsketch.Param(45.0, 0.0, 180.0, step=1.0)
    pen_width = vsketch.Param(0.3, 0.02, 10.0, step=0.02, unit="mm", decimals=2)
    merge_ends = vsketch.Param(True)
    merge_tolerance = vsketch.Param(0.0, 0.0, 20.0, step=0.05, unit="mm", decimals=2)
    zigzag_passes = vsketch.Param(1, 1, 3, step=1)
    draw_contour = vsketch.Param(True)
    draw_hatch = vsketch.Param(True)
    """Whether to fill the glyph with the hatch pattern at all (HATCH mode
    only). Paired with draw_contour: turn one off to draw only the other,
    or leave both on to draw both."""
    contour_separate_layer = vsketch.Param(False)
    contour_mode = vsketch.Param(ContourMode.OUTER.value, choices=[m.value for m in ContourMode])

    singleline_font = vsketch.Param("futural*", choices=marked_font_names())
    singleline_round_corners = vsketch.Param(False)

    def _resolve_output_path(self) -> Path:
        if self.output_path:
            return Path(self.output_path)
        p = Path(self.input_path)
        return p.with_name(p.stem + "_hatched" + p.suffix)

    def draw(self, vsk: vsketch.Vsketch) -> None:
        glyph_outlines = _cached_glyph_outlines(self.input_path)
        if glyph_outlines is None:
            vsk.size("a5", landscape=True, center=False)
            return

        target = _current_target(self.selected_layer)
        _sync_selection(type(self), self.input_path, target, self._render_params())

        try:
            self._draw_result(vsk, glyph_outlines, target)
        except Exception:
            # The interactive viewer redraws on every keystroke while editing
            # params (e.g. a half-typed font name), so a crashing draw() must
            # not take the whole GUI down — but silently swallowing the error
            # here made every subsequent redraw look identically "frozen"
            # with no indication anything was wrong. Print it instead: it
            # shows up in the terminal `fonthatch-gui` was launched from.
            # The CLI/library paths do not swallow errors at all.
            import traceback

            traceback.print_exc()
            vsk.size("a5", landscape=True, center=False)

    def _render_params(self) -> RenderParams:
        return RenderParams(
            mode=RenderMode(self.mode),
            hatch=HatchParams(
                fill_type=FillType(self.fill_type),
                spacing=self.spacing,
                fill_spacing=self.fill_spacing or None,
                inset=self.inset,
                angle=self.angle,
                pen_width=self.pen_width,
                merge_ends=self.merge_ends,
                merge_tolerance=self.merge_tolerance,
                zigzag_passes=self.zigzag_passes,
            ),
            singleline_font=unmark_font_name(self.singleline_font),
            singleline_round_corners=self.singleline_round_corners,
            draw_contour=self.draw_contour,
            draw_hatch=self.draw_hatch,
            contour_separate_layer=self.contour_separate_layer,
            contour_mode=ContourMode(self.contour_mode),
        )

    def _overrides_for(self, glyph_outlines: list[GlyphOutline]) -> dict[int, RenderParams]:
        """Keyed by id(glyph) rather than layer index (see
        add_text_hatched_layers), so that function stays agnostic of *why*
        glyphs are grouped — it just gets handed a plain per-glyph
        mapping."""
        return {
            id(glyph): _layer_overrides.get((self.input_path, glyph.run.layer_index), _default_render_params)
            for glyph in glyph_outlines
        }

    def export_full_document(self) -> tuple[vpype.Document, int]:
        """Full, unfiltered document for the "Export" button (SketchViewer.
        on_like, patched in _patch_gui_chrome). Uses the same per-layer
        overrides as the live preview (last recorded by `draw()`, via
        `_sync_selection`), so Export matches what's on screen — but never
        the selection-highlight rectangle (see `_draw_result`), since this
        never touches `vsk.document`."""
        glyph_outlines = _cached_glyph_outlines(self.input_path) or []
        doc, text_id, _, _ = build_document(
            self.input_path,
            glyph_outlines,
            _default_render_params,
            DEFAULT_QUANTIZATION,
            overrides=self._overrides_for(glyph_outlines),
            render_cache=_render_cache,
        )
        return doc, text_id

    def _draw_result(self, vsk: vsketch.Vsketch, glyph_outlines: list[GlyphOutline], target: int | None) -> None:
        doc, _text_id, hatched_id, contour_id = build_document(
            self.input_path,
            glyph_outlines,
            _default_render_params,
            DEFAULT_QUANTIZATION,
            overrides=self._overrides_for(glyph_outlines),
            render_cache=_render_cache,
        )

        width, height = doc.page_size if doc.page_size is not None else (400.0, 300.0)
        vsk.size(width, height, center=False)

        for layer_id, lc in doc.layers.items():
            vsk.document.add(lc, layer_id=layer_id)
            # Vsketch.document.add() only copies point data, not layer
            # metadata (name) — propagate what layers.py set.
            name = lc.property(vpype.METADATA_FIELD_NAME)
            if name is not None:
                vsk.document.layers[layer_id].set_property(vpype.METADATA_FIELD_NAME, name)

        # UI-only annotation showing which layer selected_layer currently
        # refers to — added straight to vsk.document (never to `doc`
        # above), so export_full_document never sees it, since that
        # rebuilds independently via build_document rather than reusing
        # vsk.document. vsketch's own native save *does* reuse vsk.document
        # though, so post_finalize hides this layer by name too, alongside
        # "text".
        highlight_lines = _selection_highlight_lines(glyph_outlines, target)
        if highlight_lines:
            highlight_id = vsk.document.free_id()
            vsk.document.add(vpype.LineCollection(highlight_lines), layer_id=highlight_id)
            vsk.document.layers[highlight_id].set_property(vpype.METADATA_FIELD_NAME, SELECTION_LAYER_NAME)
            vsk.document.layers[highlight_id].set_property(
                vpype.METADATA_FIELD_COLOR, vpype.Color(_SELECTION_HIGHLIGHT_COLOR)
            )
            vsk.penWidth(max(self.pen_width * 0.3, 0.05), highlight_id)

        # Registered via vsk.penWidth() (not by setting the Document property
        # directly): SketchClass.execute_draw() runs after draw() and
        # unconditionally overwrites every layer's pen-width property with
        # vsk.getPenWidth(layer_id), which falls back to vsketch's own
        # default for any layer that never called penWidth() — setting the
        # property by hand here would just get clobbered a moment later.
        # This is what makes vpype_viewer's PREVIEW view mode reflect the
        # actual configured pen width instead of a generic default. Uses
        # self.pen_width (whatever's currently displayed — the selected
        # layer's own override mid-edit, or the shared default otherwise)
        # rather than _default_render_params: the pen-width *property* is
        # necessarily one value for the whole layer regardless, so this is
        # purely about what the live preview shows while editing; the
        # Export button (export_full_document, above) always uses the
        # shared default for this property, independent of what's on screen.
        for lid in (hatched_id, contour_id):
            if lid is not None and lid in vsk.document.layers:
                vsk.penWidth(self.pen_width, lid)

    def reset_selected_layer(self) -> None:
        """Drops the currently selected layer's own override (if it has
        one) and snaps `selected_layer` back to 0 ("all"), same as if it
        had never been singled out. The sidebar's "Reset to Default"
        button (SketchViewer.on_reset, patched in _patch_gui_chrome) calls
        this directly against the last-completed sketch instance — a real
        click action, unlike the self-unchecking checkbox this replaced.
        No effect while nothing is selected (0 already means "all", so
        there's no override to drop)."""
        target = _current_target(self.selected_layer)
        if target is None:
            return
        _reset_target_override(self.input_path, target)
        type(self).selected_layer.set_value(0)

    def _write_output(self, doc: vpype.Document, text_id: int) -> None:
        output_path = self._resolve_output_path()
        try:
            input_resolved = Path(self.input_path).resolve()
        except OSError:
            input_resolved = None
        if input_resolved is not None and output_path.resolve() == input_resolved:
            print(f"fonthatch: refusing to save over the input file ({output_path}) — change output_path.")
            return
        try:
            write_svg(doc, str(output_path), hidden_layer_ids=[text_id])
            print(f"fonthatch: wrote {output_path}")
        except OSError as exc:
            print(f"fonthatch: could not write {output_path}: {exc}")

    def finalize(self, vsk: vsketch.Vsketch) -> None:
        pass

    def post_finalize(self, vsk: vsketch.Vsketch, path) -> None:
        try:
            if Path(self.input_path).resolve() == Path(path).resolve():
                print(f"fonthatch: refusing to save over the input file ({path}).")
                return
        except OSError:
            pass
        hide_layers_in_file(str(path), [TEXT_LAYER_NAME, SELECTION_LAYER_NAME])


def _patch_comma_decimal_input() -> None:
    """Make the parameter panel's numeric fields (spacing, pen width, ...)
    accept "0.49" and "0,49" interchangeably, regardless of the OS locale
    vsketch_cli's QDoubleSpinBox happens to pick up (French macOS uses a
    comma decimal separator by default, which otherwise silently rejects
    dot-typed values and vice versa). Subclasses rather than edits the
    installed vsketch_cli package (not ours to modify) and swaps the name
    vsketch_cli's ParamsWidget.set_params() looks up at call time, so this
    must run before that widget is ever built."""
    from vsketch_cli import param_widget

    class _CommaTolerantFloatParamWidget(param_widget.FloatParamWidget):
        def _to_widget_locale(self, text: str) -> str:
            # Only the *other* separator needs swapping — the widget's own
            # locale (e.g. comma on French systems) already works, so
            # touching it here would just break it in the opposite
            # direction.
            widget_sep = self.locale().decimalPoint()
            other_sep = "," if widget_sep == "." else "."
            return text.replace(other_sep, widget_sep)

        def validate(self, text, pos):
            from PySide6.QtWidgets import QDoubleSpinBox

            state, _, new_pos = QDoubleSpinBox.validate(self, self._to_widget_locale(text), pos)
            return state, text, new_pos

        def valueFromText(self, text):
            from PySide6.QtWidgets import QDoubleSpinBox

            return QDoubleSpinBox.valueFromText(self, self._to_widget_locale(text))

    param_widget.FloatParamWidget = _CommaTolerantFloatParamWidget


def _patch_param_widgets() -> None:
    """Two independent per-widget-class monkeypatches, both installed by
    swapping the name ``vsketch_cli.param_widget.ParamsWidget.set_params()``
    looks up at call time (must run before that widget is ever built —
    same technique ``_patch_comma_decimal_input`` above uses for
    ``FloatParamWidget``, kept separate from it since these two are
    unrelated fixes):

    - ``ChoiceParamWidget`` (used for every ``Param`` with ``choices=``,
      e.g. ``mode``, ``fill_type``, ``selected_layer``): rebuild its item
      list from the *current* choices on every ``update_from_param()``
      call instead of trusting the ones built once at construction time.
      A no-op for every choices param except ``selected_layer`` (same
      items every time, just rebuilt), whose choices and labels are
      refreshed from ``_layer_choices`` on every redraw — see module
      docstring, problem 2.
    - ``TextParamWidget`` (used for ``input_path``/``output_path``, the
      only string params with no ``choices``): see module docstring's
      final paragraph — skip the redundant ``setText()`` that otherwise
      resets the cursor to column 0 on every keystroke.
    """
    from vsketch_cli import param_widget

    class _DynamicChoiceParamWidget(param_widget.ChoiceParamWidget):
        def update_from_param(self) -> None:
            if self._param is FontHatchSketch.selected_layer:
                entries = _layer_choices
            else:
                entries = [(choice, str(choice)) for choice in (self._param.choices or ())]
            self._param.choices = tuple(value for value, _ in entries)
            self.clear()
            for value, label in entries:
                self.addItem(label, value)
            index = self.findData(self._param.value)
            self.setCurrentIndex(index if index >= 0 else 0)

    param_widget.ChoiceParamWidget = _DynamicChoiceParamWidget

    class _CursorPreservingTextParamWidget(param_widget.TextParamWidget):
        def update_from_param(self) -> None:
            new_text = str(self._param.value)
            if self.toPlainText() == new_text:
                return
            cursor = self.textCursor()
            position = cursor.position()
            self.setText(new_text)
            cursor = self.textCursor()
            cursor.setPosition(min(position, len(new_text)))
            self.setTextCursor(cursor)

    param_widget.TextParamWidget = _CursorPreservingTextParamWidget


def _patch_gui_chrome() -> None:
    """fonthatch has no use for vsketch_cli's generative-art chrome (a
    per-render random seed, "liking" a render into a numbered scratch file)
    — hatching a font is deterministic and there's exactly one output the
    user wants, at a path they chose. Rather than fork vsketch_cli, patch
    its viewer in place (must run before SketchViewer/SideBarWidget are
    instantiated):
      - hide the Seed group box and disable its 'R' randomize shortcut —
        draw() never calls anything seed-dependent, so it was always inert,
        just visible clutter.
      - move the status label ("Loading...", "Done", "ERROR (see console)")
        to the top of the sidebar, so it's visible without scrolling.
      - repurpose the "LIKE!" button/'s' shortcut as "Export": same
        proven save plumbing (it already threads the write and runs
        post_finalize to hide the "text" layer afterward), just pointed at
        this sketch's own resolved output path instead of a numbered
        "..._liked.svg" file in a fixed output dir.
      - add a real "Reset to Default" button below the parameter panel,
        replacing the old self-unchecking ``reset_to_default`` checkbox
        param — its handler (``on_reset``) just calls
        ``FontHatchSketch.reset_selected_layer()`` against the
        last-completed sketch instance and triggers a redraw.
      - refresh the parameter panel from the just-completed draw()'s Param
        values every redraw: `draw()` (via `_sync_selection`) may have
        written a different layer's own stored settings into the
        class-level Params, and `ParamsWidget.update_from_param()` — a real
        vsketch_cli method already used internally by its own config-load
        feature, just never otherwise wired to fire after a plain redraw —
        is what actually pushes that into the visible fields.
    """
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QPushButton
    from vsketch_cli import sketch_viewer

    orig_sidebar_init = sketch_viewer.SideBarWidget.__init__

    def patched_sidebar_init(self, *args, **kwargs):
        orig_sidebar_init(self, *args, **kwargs)
        self.seed_widget.setVisible(False)
        self.like_btn.setText("Export")
        self.reset_btn = QPushButton("Reset to Default")
        self.reset_btn.setEnabled(False)
        layout = self.layout()
        layout.insertWidget(layout.indexOf(self.params_widget) + 1, self.reset_btn)
        layout.removeWidget(self.status_label)
        layout.insertWidget(0, self.status_label)

    sketch_viewer.SideBarWidget.__init__ = patched_sidebar_init

    orig_viewer_init = sketch_viewer.SketchViewer.__init__

    def patched_viewer_init(self, *args, **kwargs):
        orig_viewer_init(self, *args, **kwargs)
        self._sidebar.reset_btn.clicked.connect(self.on_reset)  # type: ignore
        for shortcut in self.findChildren(QShortcut):
            if shortcut.key() == QKeySequence("R"):
                shortcut.setEnabled(False)

    sketch_viewer.SketchViewer.__init__ = patched_viewer_init

    orig_redraw_completed = sketch_viewer.SketchViewer.redraw_sketch_completed

    def patched_redraw_completed(self, sketch) -> None:
        orig_redraw_completed(self, sketch)
        self._sidebar.reset_btn.setEnabled(self._sketch is not None)
        if self._sketch is not None:
            self._sidebar.params_widget.update_from_param()

    sketch_viewer.SketchViewer.redraw_sketch_completed = patched_redraw_completed

    def patched_on_like(self) -> None:
        if self._sketch is None:
            return
        # Rebuilt fresh from disk rather than reusing self._sketch.vsk.document:
        # that's whatever the live preview last managed to put together, which
        # may be a blank a5 fallback if draw() hit an exception (caught in
        # draw(), logged, but not re-raised) or may carry vsketch's own
        # centering transform — export must always reflect the actual source
        # file and settings, independent of what the preview happens to show.
        doc, text_id = self._sketch.export_full_document()
        self._sketch._write_output(doc, text_id)
        self._sidebar.status_label.setText('<span style="color:green"><b>Exported</b></span>')

    sketch_viewer.SketchViewer.on_like = patched_on_like

    def patched_on_reset(self) -> None:
        if self._sketch is None:
            return
        self._sketch.reset_selected_layer()
        self.redraw_sketch()

    sketch_viewer.SketchViewer.on_reset = patched_on_reset


def launch() -> None:
    """Entry point for the `fonthatch-gui` console script: launches the
    vsketch interactive viewer (`vsk run`) on this module."""
    from vsketch_cli.cli import cli as vsk_cli

    _patch_comma_decimal_input()
    _patch_param_widgets()
    _patch_gui_chrome()
    vsk_cli.main(args=["run", str(Path(__file__).resolve())], prog_name="vsk", standalone_mode=True)


if __name__ == "__main__":
    FontHatchSketch.display()
