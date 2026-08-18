"""Registers `fonthatch` as a vpype command: `vpype read in.svg fonthatch ... write out.svg`.

A vpype global-processor command only receives the (already-read)
``vpype.Document`` — by the time it runs, the upstream `read` command has
already dropped `<text>` elements, since vpype's own SVG reader doesn't
handle text at all (see ``core.svg_text``). To recover the original text,
this command reads the source path back out of the document's
``METADATA_FIELD_SOURCE`` property (set by vpype's `read` command) and
re-parses *that* file for text independently, then adds "text"/"hatched"
layers onto the document that was passed in — leaving whatever upstream
commands already did to its other layers untouched.
"""

from __future__ import annotations

from pathlib import Path

import click
import vpype
import vpype_cli

from ..core.hatch import ContourMode, FillType, HatchParams
from ..core.layers import add_text_hatched_layers
from ..core.pipeline import extract_glyph_outlines
from ..core.render import RenderMode, RenderParams
from ..core.accents import marked_font_names, unmark_font_name
from ..core.svg_output import write_svg


@click.command()
@click.option(
    "--mode",
    type=vpype_cli.ChoiceType([m.value for m in RenderMode]),
    default=RenderMode.HATCH.value,
    show_default=True,
    help="hatch: keep the outline, fill it. singleline: replace the glyph with a single-stroke font.",
)
@click.option(
    "--fill-type",
    type=vpype_cli.ChoiceType([f.value for f in FillType]),
    default=FillType.SPIRALING.value,
    show_default=True,
    help="Hatch fill style (--mode hatch only).",
)
@click.option("--spacing", type=vpype_cli.LengthType(), default="1mm", show_default=True, help="Spacing for lines/crosshatch/concentric.")
@click.option(
    "--fill-spacing",
    type=vpype_cli.LengthType(),
    default=None,
    help="Line-to-line spacing for the coverage fills (spiraling/zigzag/glyph-fill). "
    "Defaults to the pen width, i.e. adjacent strokes exactly tangent.",
)
@click.option("--inset", type=vpype_cli.LengthType(), default="0mm", show_default=True, help="Shrink the outline before filling.")
@click.option("--angle", type=vpype_cli.AngleType(), default="45deg", show_default=True, help="Hatch angle (lines/crosshatch).")
@click.option(
    "--pen-width",
    type=vpype_cli.LengthType(),
    default="0.3mm",
    show_default=True,
    help="Physical pen width; drives tangent-ring spacing for spiraling.",
)
@click.option(
    "--merge-ends/--no-merge-ends",
    default=True,
    show_default=True,
    help="Thread consecutive rings/scanlines into one continuous stroke.",
)
@click.option(
    "--zigzag-passes",
    type=vpype_cli.IntRangeType(1, 3),
    default=1,
    show_default=True,
    help="Number of zigzag passes, evenly spread across 180 degrees (--fill-type zigzag only).",
)
@click.option(
    "--merge-tolerance",
    type=vpype_cli.LengthType(),
    default="0mm",
    show_default=True,
    help="How far a merge-ends bridge may stray outside the glyph before it's rejected.",
)
@click.option(
    "--guarantee-coverage/--no-guarantee-coverage",
    default=True,
    show_default=True,
    help="Run the top-up pass that guarantees complete coverage on the coverage fills "
    "(spiraling/zigzag/glyph-fill). Turn off to let a wide --fill-spacing plot as the "
    "open, faster pattern it sets instead of being filled back in solid.",
)
@click.option(
    "--font",
    "singleline_font",
    type=vpype_cli.ChoiceType(marked_font_names()),
    default="futural*",
    show_default=True,
    help="Single-stroke font (--mode singleline only). '*' marks fonts with accented glyphs.",
)
@click.option(
    "--round-corners/--no-round-corners",
    default=False,
    show_default=True,
    help="Smooth the substitute glyph's strokes for a rounded look (--mode singleline only).",
)
@click.option(
    "--draw-contour/--no-draw-contour",
    default=True,
    show_default=True,
    help="Trace the glyph's true outline at all (--mode hatch only).",
)
@click.option(
    "--draw-hatch/--no-draw-hatch",
    default=True,
    show_default=True,
    help="Fill the glyph with the hatch pattern at all (--mode hatch only).",
)
@click.option(
    "--contour-separate-layer/--no-contour-separate-layer",
    default=False,
    show_default=True,
    help='Put the contour on its own "contour" layer instead of merging it into "hatched".',
)
@click.option(
    "--contour-mode",
    type=vpype_cli.ChoiceType([m.value for m in ContourMode]),
    default=ContourMode.OUTER.value,
    show_default=True,
    help="centerline: pen straddles the true outline. outer: pen's outer edge traces it (offset inward by pen-width/2).",
)
@vpype_cli.global_processor
def fonthatch(
    document: vpype.Document,
    mode: str,
    fill_type: str,
    spacing: float,
    fill_spacing: float | None,
    inset: float,
    angle: float,
    pen_width: float,
    merge_ends: bool,
    zigzag_passes: int,
    merge_tolerance: float,
    guarantee_coverage: bool,
    singleline_font: str,
    round_corners: bool,
    draw_contour: bool,
    draw_hatch: bool,
    contour_separate_layer: bool,
    contour_mode: str,
) -> vpype.Document:
    """Isolate SVG <text> into a hidden "text" layer and a hatch-filled (or
    single-stroke-font-substituted) "hatched" layer."""
    source = document.property(vpype.METADATA_FIELD_SOURCE)
    if not source:
        raise click.UsageError(
            "fonthatch needs a document read from an SVG file — use `vpype read FILE.svg fonthatch ...`"
        )

    glyph_outlines = extract_glyph_outlines(str(source))
    params = RenderParams(
        mode=RenderMode(mode),
        hatch=HatchParams(
            fill_type=FillType(fill_type),
            spacing=spacing,
            fill_spacing=fill_spacing,
            inset=inset,
            angle=angle,
            pen_width=pen_width,
            merge_ends=merge_ends,
            zigzag_passes=zigzag_passes,
            merge_tolerance=merge_tolerance,
            guarantee_coverage=guarantee_coverage,
        ),
        singleline_font=unmark_font_name(singleline_font),
        singleline_round_corners=round_corners,
        draw_contour=draw_contour,
        draw_hatch=draw_hatch,
        contour_separate_layer=contour_separate_layer,
        contour_mode=ContourMode(contour_mode),
    )
    add_text_hatched_layers(document, glyph_outlines, params)
    return document


@click.command()
@click.argument("output_svg", type=click.Path(dir_okay=False))
@click.option(
    "--hide-name",
    "hide_names",
    multiple=True,
    default=("text",),
    show_default=True,
    help="Layer name(s) to write hidden (display:none) rather than deleting.",
)
@vpype_cli.global_processor
def fonthatch_write(document: vpype.Document, output_svg: str, hide_names: tuple[str, ...]) -> vpype.Document:
    """Write OUTPUT_SVG, hiding any layer whose name matches --hide-name.

    vpype's own `write` command has no hidden-layer support (every layer is
    always written `display:inline`), so a `fonthatch ... fonthatch-write`
    pipeline needs this in place of the builtin `write` to actually hide the
    "text" layer.
    """
    source = document.property(vpype.METADATA_FIELD_SOURCE)
    if source and Path(str(source)).resolve() == Path(output_svg).resolve():
        raise click.UsageError(
            f"Refusing to write output over the input file ({source}) — choose a different output path."
        )
    hidden_ids = [
        layer_id
        for layer_id, lc in document.layers.items()
        if lc.property(vpype.METADATA_FIELD_NAME) in hide_names
    ]
    write_svg(document, output_svg, hidden_layer_ids=hidden_ids)
    return document
