"""Standalone one-shot CLI: ``fonthatch in.svg out.svg [options]``."""

from __future__ import annotations

import click
import vpype

from ..core.hatch import ContourMode, FillType, HatchParams
from ..core.pipeline import process_svg
from ..core.render import RenderMode, RenderParams
from ..core.accents import marked_font_names, unmark_font_name


def _length(ctx, param, value):
    if value is None:
        return None
    try:
        return vpype.convert_length(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


@click.command()
@click.argument("input_svg", type=click.Path(exists=True, dir_okay=False))
@click.argument("output_svg", type=click.Path(dir_okay=False))
@click.option(
    "--mode",
    type=click.Choice([m.value for m in RenderMode]),
    default=RenderMode.HATCH.value,
    show_default=True,
    help="hatch: keep the outline, fill it. singleline: replace the glyph with a single-stroke font.",
)
@click.option(
    "--fill-type",
    type=click.Choice([f.value for f in FillType]),
    default=FillType.SPIRALING.value,
    show_default=True,
    help="Hatch fill style (--mode hatch only).",
)
@click.option("--spacing", default="1mm", show_default=True, callback=_length, help="Spacing for lines/crosshatch/concentric.")
@click.option(
    "--fill-spacing",
    default=None,
    callback=_length,
    help="Line-to-line spacing for the coverage fills (spiraling/zigzag/glyph-fill). "
    "Defaults to the pen width (adjacent strokes exactly tangent) with --guarantee-coverage, "
    "or a wider open spacing with --no-guarantee-coverage.",
)
@click.option("--inset", default="0mm", show_default=True, callback=_length, help="Shrink the outline before filling.")
@click.option("--angle", default=45.0, type=float, show_default=True, help="Hatch angle in degrees (lines/crosshatch).")
@click.option(
    "--pen-width",
    default="0.3mm",
    show_default=True,
    callback=_length,
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
    default=1,
    type=click.IntRange(1, 3),
    show_default=True,
    help="Number of zigzag passes, evenly spread across 180 degrees (--fill-type zigzag only).",
)
@click.option(
    "--merge-tolerance",
    default="0mm",
    show_default=True,
    callback=_length,
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
    type=click.Choice(marked_font_names()),
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
    type=click.Choice([m.value for m in ContourMode]),
    default=ContourMode.OUTER.value,
    show_default=True,
    help="centerline: pen straddles the true outline. outer: pen's outer edge traces it (offset inward by pen-width/2).",
)
def main(
    input_svg,
    output_svg,
    mode,
    fill_type,
    spacing,
    fill_spacing,
    inset,
    angle,
    pen_width,
    merge_ends,
    zigzag_passes,
    merge_tolerance,
    guarantee_coverage,
    singleline_font,
    round_corners,
    draw_contour,
    draw_hatch,
    contour_separate_layer,
    contour_mode,
):
    """Hide every <text> element in INPUT_SVG in place and, for each of its
    own top-level layers that has text, add a new hatch-filled (or
    single-stroke-font-substituted) "<layer> hatched" layer (and, with
    --contour-separate-layer, a "<layer> contour" one) right after it.
    Everything else in INPUT_SVG — shapes, styling, other layers, structure —
    passes through to OUTPUT_SVG completely unchanged."""
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
    process_svg(input_svg, output_svg, params)
    click.echo(f"Wrote {output_svg}")


if __name__ == "__main__":
    main()
