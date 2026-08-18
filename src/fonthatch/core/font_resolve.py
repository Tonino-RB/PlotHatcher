"""Resolve an SVG font-family/weight/style triple to an on-disk font file.

Cross-platform via ``matplotlib.font_manager``, which scans system font
directories on macOS/Windows and uses fontconfig where available on Linux —
the most battle-tested option available without adding a native fontconfig
dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import matplotlib.font_manager as fm
import uharfbuzz as hb

_VALID_STYLES = {"normal", "italic", "oblique"}

_FALLBACK_FAMILIES = (
    # Broad-coverage families tried, in order, for any character the
    # requested font's own cmap doesn't map — e.g. a CJK <text> run whose
    # SVG-specified family (e.g. "PingFang SC") isn't a plain font *file*
    # matplotlib's directory-scanning font_manager can discover, even
    # though the CoreText-based apps that authored the SVG rendered it fine
    # via OS-level per-character font substitution. Ordered roughly
    # CJK-first (the common case for a font_manager miss on macOS) down to
    # bundled-with-matplotlib DejaVu Sans as the last resort.
    "Arial Unicode MS",
    "Noto Sans CJK SC",
    "Noto Sans",
    "PingFang SC",
    "Heiti SC",
    "STHeiti",
    "Songti SC",
    "Hiragino Sans GB",
    "Hiragino Sans",
    "Apple SD Gothic Neo",
    "Segoe UI",
    "Segoe UI Symbol",
    "DejaVu Sans",
)


@dataclass(frozen=True)
class ResolvedFont:
    requested_family: str
    matched_family: str
    path: str
    face_index: int = 0
    """Face index within a TrueType/OpenType collection (.ttc/.otc), else 0."""


def _split_family_list(font_family: str) -> list[str]:
    parts = [p.strip().strip('"').strip("'") for p in font_family.split(",")]
    return [p for p in parts if p]


def _normalize_weight(weight: str) -> str:
    weight = (weight or "normal").strip().lower()
    if weight in ("normal", "bold", "bolder", "lighter") or weight.isdigit():
        return weight
    return "normal"


def _normalize_style(style: str) -> str:
    style = (style or "normal").strip().lower()
    return style if style in _VALID_STYLES else "normal"


@lru_cache(maxsize=None)
def resolve_font(font_family: str, font_weight: str = "normal", font_style: str = "normal") -> ResolvedFont:
    """Resolve a (possibly comma-separated, CSS-style) font-family list."""
    families = _split_family_list(font_family) or ["sans-serif"]
    weight = _normalize_weight(font_weight)
    style = _normalize_style(font_style)

    for family in families:
        try:
            props = fm.FontProperties(family=family, weight=weight, style=style)
            path = fm.findfont(props, fallback_to_default=False)
            return ResolvedFont(
                requested_family=font_family,
                matched_family=family,
                path=str(path),
                face_index=getattr(path, "face_index", 0),
            )
        except ValueError:
            continue

    fallback_family = families[-1]
    props = fm.FontProperties(family=fallback_family, weight=weight, style=style)
    path = fm.findfont(props, fallback_to_default=True)
    return ResolvedFont(
        requested_family=font_family,
        matched_family=fallback_family,
        path=str(path),
        face_index=getattr(path, "face_index", 0),
    )


@lru_cache(maxsize=None)
def _has_glyph(path: str, face_index: int, codepoint: int) -> bool:
    blob = hb.Blob.from_file_path(path)
    font = hb.Font(hb.Face(blob, face_index))
    return font.get_nominal_glyph(codepoint) is not None


@lru_cache(maxsize=None)
def _fallback_candidates(weight: str, style: str) -> tuple[ResolvedFont, ...]:
    """``_FALLBACK_FAMILIES`` resolved once per weight/style, deduplicated by
    the file they actually land on (several family names above commonly
    resolve to the same font, e.g. every CJK family collapsing to whatever
    matplotlib's own default already is when none of them are found).

    Most of these families won't exist on any given system — that's the
    point of trying several — so matplotlib's "family not found" warning is
    expected noise here, not a real problem; suppressed for the duration."""
    font_manager_log = logging.getLogger("matplotlib.font_manager")
    previous_level = font_manager_log.level
    font_manager_log.setLevel(logging.ERROR)
    try:
        seen_paths: set[tuple[str, int]] = set()
        candidates = []
        for family in _FALLBACK_FAMILIES:
            resolved = resolve_font(family, weight, style)
            key = (resolved.path, resolved.face_index)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            candidates.append(resolved)
        return tuple(candidates)
    finally:
        font_manager_log.setLevel(previous_level)


def resolve_fallback_font(
    codepoint: int, font_weight: str = "normal", font_style: str = "normal"
) -> ResolvedFont | None:
    """First font among ``_FALLBACK_FAMILIES`` (available on this system)
    whose cmap actually maps ``codepoint`` — used when a run's own resolved
    font doesn't cover a character (HarfBuzz shapes it to ``.notdef``,
    normally a visible tofu box). ``None`` if nothing in the cascade covers
    it either."""
    weight = _normalize_weight(font_weight)
    style = _normalize_style(font_style)
    for candidate in _fallback_candidates(weight, style):
        if _has_glyph(candidate.path, candidate.face_index, codepoint):
            return candidate
    return None
