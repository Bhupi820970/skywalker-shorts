"""Letterbox detection strips black bars only and never cuts picture."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import letterbox as pipeline


def _lum(h=1080, y1=138, y2=941, bar=4, pic=80):
    lum = np.full(h, float(bar), dtype=np.float32)
    lum[y1:y2 + 1] = float(pic)
    return lum


def test_content_span_and_black_bars():
    lum = _lum()
    span = pipeline.letterbox_content_span(lum)
    assert span == (138, 941)
    assert pipeline.letterbox_bars_are_black(lum, 138, 941) is True
    # Content leaking into the bar must not be treated as a black bar.
    leak = lum.copy()
    leak[20:40] = 80
    assert pipeline.letterbox_bars_are_black(leak, 138, 941) is False


def test_refine_widens_when_bar_is_not_black():
    # First samples look like 1920x804 in a 1080 frame; a later sample has
    # picture higher up. Crop must widen (keep more picture), never shrink.
    full = _lum(y1=80, y2=1000)
    tight = _lum(y1=138, y2=941)
    w, h, x, y = pipeline.refine_letterbox_crop(
        1920, 1080, [(138, 941), (138, 941)], [tight, tight, full],
    )
    assert x == 0
    assert y <= 80
    assert y + h >= 1000
    assert h > 804


def test_compressed_black_bars_still_count_as_black():
    # Limited-range "black" often sits near luma 16.
    lum = _lum(bar=16, pic=90)
    assert pipeline.letterbox_bars_are_black(lum, 138, 941) is True
    w, h, x, y = pipeline.refine_letterbox_crop(1920, 1080, [(138, 941)], [lum])
    assert y == pipeline.even(138)
    assert h >= 800
    assert h < 1080


def test_no_spans_keeps_full_frame():
    assert pipeline.refine_letterbox_crop(1920, 1080, [], []) == (1920, 1080, 0, 0)
