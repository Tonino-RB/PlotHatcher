"""Generate A3 specimen sheets: the whole character set, at two sizes, one
sheet per fill type.

Each sheet is a real plotter file — the same pipeline the CLI uses, written
through vpype — not a preview, so what you see is what the pen will draw.
Run it, plot one sheet per algorithm, and compare them on paper at the size
you actually letter at; the numbers printed alongside (strokes, drawn length,
pen-up travel) are the ones that decide how long a plot takes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import vpype

from fonthatch.core.hatch import ContourMode, FillType, HatchParams
from fonthatch.core.layers import build_document
from fonthatch.core.pipeline import extract_glyph_outlines
from fonthatch.core.render import RenderMode, RenderParams
from fonthatch.core.svg_output import write_svg

MM = vpype.convert_length("1mm")

PAGE_W_MM, PAGE_H_MM = 420.0, 297.0  # A3 landscape
MARGIN_MM = 16.0

ROWS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789 &@?!.,;:-+=()",
    "Hamburgefonstiv",
)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _input_svg(path: Path, blocks: list[tuple[str, float, float, float]], font: str) -> None:
    """blocks: (text, x_mm, baseline_y_mm, font_size_mm)."""
    body = "\n".join(
        f'  <text x="{x * MM:.4f}" y="{y * MM:.4f}" font-family="{font}" '
        f'font-size="{size * MM:.4f}">{_escape(text)}</text>'
        for text, x, y, size in blocks
    )
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W_MM}mm" height="{PAGE_H_MM}mm" '
        f'viewBox="0 0 {PAGE_W_MM * MM:.4f} {PAGE_H_MM * MM:.4f}">\n{body}\n</svg>\n',
        encoding="utf-8",
    )


def _row_width_mm(text: str, size_mm: float, font: str, tmp_dir: Path) -> float:
    """Width the row actually shapes to, measured through the real pipeline —
    so the sheet fits whatever font is asked for rather than a guess at its
    average advance."""
    probe = tmp_dir / "_probe.svg"
    _input_svg(probe, [(text, 0.0, size_mm * 2, size_mm)], font)
    try:
        outlines = extract_glyph_outlines(str(probe))
        xs = [o.polygon.bounds[2] for o in outlines if o.polygon is not None and not o.polygon.is_empty]
        return (max(xs) / MM) if xs else 0.0
    finally:
        probe.unlink(missing_ok=True)


def _fit_size(size_mm: float, font: str, tmp_dir: Path) -> float:
    """Shrink a requested size until the widest row clears the margins."""
    usable = PAGE_W_MM - 2 * MARGIN_MM - 32.0  # right-hand strip reserved for the size label
    widest = max(_row_width_mm(row, size_mm, font, tmp_dir) for row in ROWS)
    return size_mm if widest <= usable else size_mm * usable / widest


def _layout(large_mm: float, small_mm: float) -> tuple[list[tuple[str, float, float, float]], list[tuple[float, float]]]:
    """Place both character sets down the page, spreading the leftover height
    evenly between rows so the sheet reads as a specimen rather than a block
    of text pushed to the top. Returns the text blocks and, for each size, the
    y of its first row so the size label can sit beside it."""
    sizes = (large_mm, small_mm)
    content = sum(size * 1.35 * len(ROWS) for size in sizes)
    top = MARGIN_MM + 24.0
    # The last row's baseline must clear the bottom margin by its own descender.
    available = PAGE_H_MM - MARGIN_MM - small_mm * 0.3 - top
    slots = len(sizes) * len(ROWS) + 1  # one gap before each row, one between the blocks
    gap = max(available - content, 0.0) / slots

    blocks: list[tuple[str, float, float, float]] = []
    marks: list[tuple[float, float]] = []
    y = top
    for block_index, size in enumerate(sizes):
        if block_index:
            y += gap
        marks.append((y + size * 1.35, size))
        for row in ROWS:
            y += size * 1.35 + gap
            blocks.append((row, MARGIN_MM, y, size))
    return blocks, marks


def _labels(fill: FillType, pen_mm: float, stats: str, marks: list[tuple[float, float]]) -> vpype.LineCollection:
    lc = vpype.LineCollection()

    def put(text: str, x_mm: float, y_mm: float, size_mm: float) -> None:
        block = vpype.text_line(text, font_name="futural", size=size_mm * MM)
        block.translate(x_mm * MM, y_mm * MM)
        lc.extend(block)

    put(f"{fill.value.upper()}   pen {pen_mm:g}mm", MARGIN_MM, MARGIN_MM + 7.0, 6.0)
    put(stats, MARGIN_MM, MARGIN_MM + 14.5, 3.0)
    for y_mm, size_mm in marks:
        put(f"{size_mm:.0f}mm", PAGE_W_MM - MARGIN_MM - 20.0, y_mm, 4.0)
    return lc


def build_sheet(fill: FillType, out: Path, *, font: str, pen_mm: float, large_mm: float, small_mm: float,
                spacing_mm: float, tmp_dir: Path) -> str:
    pen = pen_mm * MM
    large_mm = _fit_size(large_mm, font, tmp_dir)
    small_mm = _fit_size(small_mm, font, tmp_dir)
    blocks, marks = _layout(large_mm, small_mm)
    src = tmp_dir / f"_specimen_{fill.value}.svg"
    _input_svg(src, blocks, font)

    params = RenderParams(
        mode=RenderMode.HATCH,
        hatch=HatchParams(
            fill_type=fill,
            spacing=spacing_mm * MM,
            pen_width=pen,
            merge_ends=True,
            angle=45.0,
        ),
        draw_contour=True,
        draw_hatch=True,
        contour_mode=ContourMode.OUTER,
    )
    outlines = extract_glyph_outlines(str(src))
    doc, text_id, hatched_id, _ = build_document(str(src), outlines, params)

    lines = doc.layers[hatched_id]
    n = len(lines)
    draw_mm = sum(abs(line[1:] - line[:-1]).sum() for line in lines) / MM if n else 0.0
    up_mm = sum(abs(b[0] - a[-1]) for a, b in zip(lines, lines[1:])) / MM if n > 1 else 0.0
    stats = f"{n} strokes   {draw_mm:.0f}mm drawn   {up_mm:.0f}mm pen-up"

    doc.add(_labels(fill, pen_mm, stats, marks), layer_id=doc.free_id())
    doc.page_size = (PAGE_W_MM * MM, PAGE_H_MM * MM)
    write_svg(doc, str(out), hidden_layer_ids=[text_id], page_size=doc.page_size, center=False)
    src.unlink(missing_ok=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="specimens", type=Path)
    ap.add_argument("--font", default="Helvetica")
    ap.add_argument("--pen", default=0.5, type=float, help="pen width in mm")
    ap.add_argument("--large", default=24.0, type=float, help="large sample font size in mm")
    ap.add_argument("--small", default=11.0, type=float, help="small sample font size in mm")
    ap.add_argument("--spacing", default=1.0, type=float, help="pattern spacing in mm (concentric/lines/crosshatch)")
    ap.add_argument("--fills", nargs="*", default=[f.value for f in FillType])
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.fills:
        fill = FillType(name)
        path = args.out / f"specimen_{fill.value}_pen{args.pen:g}mm_A3.svg"
        stats = build_sheet(
            fill, path, font=args.font, pen_mm=args.pen, large_mm=args.large,
            small_mm=args.small, spacing_mm=args.spacing, tmp_dir=args.out,
        )
        print(f"{fill.value:12s} -> {path}   {stats}")


if __name__ == "__main__":
    main()
