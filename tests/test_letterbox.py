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


def test_compressed_black_span_is_picture_not_full_frame():
    """Limited-range black (luma ~16) must be treated as a bar, not as picture.

    demo-v7-dandadan missed the letterbox because CONTENT_LUMA was 14, so
    compressed-black rows counted as content and the crop stayed 1920x1080.
    """
    lum = _lum(bar=16, pic=90)
    span = pipeline.letterbox_content_span(lum)
    assert span is not None
    assert span[0] == 138
    assert span[1] == 941
    w, h, x, y = pipeline.refine_letterbox_crop(1920, 1080, [span], [lum])
    assert x == 0
    assert y >= 130
    assert h < 1080
    assert h >= 800
    # Stripping those rows is removing bars, not cropping picture.
    assert pipeline.letterbox_bars_are_black(lum, span[0], span[1]) is True


def test_true_picture_near_the_edge_is_not_stripped():
    # A frame that is picture almost edge-to-edge must stay full-frame.
    lum = _lum(y1=2, y2=1077, bar=16, pic=90)
    span = pipeline.letterbox_content_span(lum)
    assert span is not None
    w, h, x, y = pipeline.refine_letterbox_crop(1920, 1080, [span], [lum])
    # Too close to the edges → keep the full frame (do not shave picture).
    assert (w, h, x, y) == (1920, 1080, 0, 0)


def test_no_crop_alignment_ok_allows_small_stab_shift_when_corr_is_high():
    # The dandadan no_crop fail: corr 0.971, edge 0.961, shift 3.87px.
    assert pipeline.no_crop_alignment_ok(0.971, 0.961, 3.87) is True
    # A real crop/zoom would not stay this correlated at a large shift.
    assert pipeline.no_crop_alignment_ok(0.40, 0.40, 3.87) is False
    assert pipeline.no_crop_alignment_ok(0.80, 0.70, 12.0) is False
    assert pipeline.no_crop_alignment_ok(None, 0.9, 1.0) is False
