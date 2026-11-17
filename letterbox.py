"""Strip black bars only; never cut picture content."""
from __future__ import annotations

import numpy as np

from layout import even

# A row is picture if mean luma is above CONTENT. Compressed "black" often
# sits near 16, so CONTENT must sit above that (and above BAR_MAX_MEAN) or
# letterbox_content_span will treat the bars as picture and skip the strip.
LB_CONTENT_LUMA = 24
LB_BAR_MAX_MEAN = 22
LB_BAR_P90 = 28


def letterbox_content_span(lum, content_luma: float = LB_CONTENT_LUMA):
    """Return (first, last) content row, or None if the frame is too empty."""
    if lum is None or getattr(lum, "size", 0) < 20:
        return None
    rows = np.where(np.asarray(lum) > float(content_luma))[0]
    if len(rows) < 20:
        return None
    return int(rows[0]), int(rows[-1])


def letterbox_bars_are_black(lum, y1: int, y2: int, *, bar_max: float = LB_BAR_MAX_MEAN,
                             p90: float = LB_BAR_P90) -> bool:
    """True when regions outside [y1, y2] look like black bars, not picture."""
    lum = np.asarray(lum)
    h = int(lum.shape[0])
    y1 = max(0, int(y1))
    y2 = min(h - 1, int(y2))
    regions = []
    if y1 > 0:
        regions.append(lum[:y1])
    if y2 + 1 < h:
        regions.append(lum[y2 + 1:])
    if not regions:
        return True
    for bar in regions:
        if bar.size == 0:
            continue
        if float(np.mean(bar)) >= float(bar_max) or float(np.percentile(bar, 90)) >= float(p90):
            return False
    return True


def refine_letterbox_crop(sw: int, sh: int, spans: list[tuple[int, int]],
                          frame_lumas: list | None = None) -> tuple[int, int, int, int]:
    """Union of content spans, then widen if a 'bar' is not actually black.

    Never shrinks into picture: we take min(top)/max(bottom) and only expand
    the kept region when a sample has content in the proposed bar.
    """
    if not spans:
        return sw, sh, 0, 0
    y1 = min(a for a, _b in spans)
    y2 = max(b for _a, b in spans)
    for lum in frame_lumas or []:
        if lum is None:
            continue
        if letterbox_bars_are_black(lum, y1, y2):
            continue
        extra = letterbox_content_span(lum)
        if extra is None:
            return sw, sh, 0, 0
        y1 = min(y1, extra[0])
        y2 = max(y2, extra[1])
    y1 = even(max(0, y1))
    h = even(y2 - y1 + 1)
    if y1 + h > sh:
        h = even(sh - y1)
    if h < sh * 0.5 or h <= 0:
        return sw, sh, 0, 0
    if y1 <= 2 and y1 + h >= sh - 2:
        return sw, sh, 0, 0
    return sw, h, 0, y1

def no_crop_alignment_ok(frame_corr, edge_corr, shift_px,
                         *, corr_min: float = 0.75, edge_min: float = 0.6,
                         shift_max: float = 3.0, high_corr: float = 0.93,
                         shift_max_high_corr: float = 6.0) -> bool:
    """True when the plate matches the source (no zoom/crop of picture).

    A 3-6px phase-correlate shift with very high frame/edge correlation is
    typical of vidstab / scaling, not of a cropped picture.
    """
    try:
        c = float(frame_corr)
        e = float(edge_corr)
        s = float(shift_px)
    except (TypeError, ValueError):
        return False
    if c > high_corr and e > 0.9:
        return s < shift_max_high_corr
    return c > corr_min and e > edge_min and s < shift_max
