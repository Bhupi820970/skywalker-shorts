"""Hook bar sits directly above the vertically centered, uncropped plate."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import layout


def test_bar_at_top_and_landscape_plate_is_vertically_centered():
    lay = layout.whitebar_layout(608, 180, 1080)
    assert lay["bar_y"] + lay["bar_h"] == lay["plate_y"]  # hook sits right on top of the video
    assert lay["bar_y"] > 0
    assert lay["plate_y"] + lay["plate_h"] <= layout.H
    assert lay["plate_w"] == layout.W
    assert lay["plate_h"] == 608
    assert abs((1080 / 608) - (lay["plate_w"] / lay["plate_h"])) < 0.01
    ideal = layout.even((layout.H - 608) // 2)
    assert abs(lay["plate_y"] - ideal) <= 2


def test_tall_plate_is_fit_not_cropped_and_sits_under_the_bar():
    lay = layout.whitebar_layout(2000, 200, 1080)
    assert lay["bar_y"] == 0
    assert lay["plate_h"] <= lay["region_h"]
    assert lay["plate_w"] < layout.W
    assert lay["plate_y"] >= lay["bar_h"]
    assert lay["plate_y"] + lay["plate_h"] <= layout.H
    assert abs((1080 / 2000) - (lay["plate_w"] / lay["plate_h"])) < 0.02
