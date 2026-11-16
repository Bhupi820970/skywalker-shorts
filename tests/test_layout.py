"""Hook bar is a top band above an uncropped plate."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import layout


def test_bar_is_a_top_band_and_plate_is_flush_and_uncropped():
    lay = layout.whitebar_layout(608, 180, 1080)
    assert lay["bar_y"] == 0
    assert lay["plate_y"] == lay["bar_h"]
    assert lay["bar_h"] + lay["region_h"] == layout.H
    assert lay["plate_w"] == layout.W
    assert lay["plate_h"] == 608
    assert abs((1080 / 608) - (lay["plate_w"] / lay["plate_h"])) < 0.01


def test_tall_plate_is_fit_not_cropped():
    lay = layout.whitebar_layout(2000, 200, 1080)
    assert lay["bar_y"] == 0
    assert lay["plate_h"] <= lay["region_h"]
    assert lay["plate_w"] < layout.W
    assert abs((1080 / 2000) - (lay["plate_w"] / lay["plate_h"])) < 0.02
