"""White hook bar sits flush on the centered, uncropped plate (David / cyberpsychosis look)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import layout


def test_bar_at_top_and_landscape_plate_is_vertically_centered():
    lay = layout.whitebar_layout(608, 180, 1080)
    assert lay["bar_y"] + lay["bar_h"] == lay["plate_y"]  # hook sits right on top of the video
    assert lay["bar_y"] > 0  # blur, not the bar, fills the top of the canvas
    assert lay["plate_y"] + lay["plate_h"] <= layout.H
    assert lay["plate_w"] == layout.W
    assert lay["plate_h"] == 608
    assert abs((1080 / 608) - (lay["plate_w"] / lay["plate_h"])) < 0.01
    ideal = layout.even((layout.H - 608) // 2)
    assert abs(lay["plate_y"] - ideal) <= 2
    assert layout.geometry_ok(lay) and layout.bar_touches_plate(lay)


def test_tall_plate_is_fit_not_cropped_and_sits_under_the_bar():
    lay = layout.whitebar_layout(2000, 200, 1080)
    assert lay["bar_y"] == 0
    assert lay["plate_h"] <= lay["region_h"]
    assert lay["plate_w"] < layout.W
    assert lay["plate_y"] >= lay["bar_h"]
    assert lay["plate_y"] + lay["plate_h"] <= layout.H
    assert lay["bar_y"] + lay["bar_h"] == lay["plate_y"]
    assert abs((1080 / 2000) - (lay["plate_w"] / lay["plate_h"])) < 0.02
    assert layout.geometry_ok(lay)


def test_geometry_ok_rejects_a_gap_between_bar_and_plate():
    """The Interstellar failure: hook bar stuck at y=0, blur gap, then the scene."""
    good = layout.whitebar_layout(608, 180, 1080)
    assert layout.geometry_ok(good)
    bad = dict(good)
    bad["bar_y"] = 0  # stuck under the phone UI, with a gap before the plate
    assert bad["bar_y"] + bad["bar_h"] < bad["plate_y"]
    assert not layout.bar_touches_plate(bad)
    assert not layout.geometry_ok(bad)


def test_compose_blur_fill_and_flush_white_bar():
    """Still frame: blur above the bar, solid white bar touching the plate, plate uncropped."""
    # Distinctive landscape plate: green body, red top edge, blue bottom edge.
    plate = Image.new("RGB", (1080, 608), (20, 140, 40))
    draw = ImageDraw.Draw(plate)
    draw.rectangle([0, 0, 1079, 16], fill=(200, 20, 20))
    draw.rectangle([0, 592, 1079, 607], fill=(20, 20, 200))
    bar = Image.new("RGB", (1080, 180), (255, 255, 255))
    ImageDraw.Draw(bar).rectangle([200, 60, 880, 120], fill=(0, 0, 0))
    canvas, lay = layout.compose_whitebar_frame(plate, bar, blur_radius=8)
    assert canvas.size == (layout.W, layout.H)
    assert layout.geometry_ok(lay)
    assert lay["bar_y"] > 0
    assert lay["bar_y"] + lay["bar_h"] == lay["plate_y"]
    arr = np.asarray(canvas)
    # Area above the bar is a darkened blur of the plate, not a flat grey/white/black band.
    above = arr[0:lay["bar_y"], :, :]
    assert above.size
    mean = above.mean(axis=(0, 1))
    assert mean.max() < 200 and mean.min() > 4  # not white, not a black letterbox
    # Row variance: a solid grey band would be nearly flat; blur of a colorful plate is not.
    row_std = above.reshape(-1, 3).std(axis=0).mean()
    assert row_std > 5
    # Bar itself is white (our ink is a black block in the middle).
    barr = arr[lay["bar_y"]:lay["bar_y"] + lay["bar_h"], :, :]
    white = float((barr.mean(axis=2) > 230).mean())
    ink = float((barr.mean(axis=2) < 40).mean())
    assert white > 0.6 and ink > 0.01
    # Top edge of the plate (red strip) sits immediately under the bar — no gap.
    top = arr[lay["plate_y"]:lay["plate_y"] + 12, lay["plate_x"]:lay["plate_x"] + lay["plate_w"], :]
    assert top[:, :, 0].mean() > 150 and top[:, :, 1].mean() < 80
    # Full plate height is visible (blue bottom strip still there) — no crop.
    bot = arr[lay["plate_y"] + lay["plate_h"] - 12:lay["plate_y"] + lay["plate_h"],
              lay["plate_x"]:lay["plate_x"] + lay["plate_w"], :]
    assert bot[:, :, 2].mean() > 150 and bot[:, :, 0].mean() < 80
    # Space below the plate is also blur, not a white bar.
    if lay["plate_y"] + lay["plate_h"] < layout.H - 8:
        below = arr[lay["plate_y"] + lay["plate_h"]:, :, :]
        assert float((below.mean(axis=2) > 230).mean()) < 0.05



def test_mux_overlays_bar_at_computed_y():
    """The stacked mux must place the bar at lay['bar_y'], never hard-coded y=0."""
    src = (ROOT / "pipeline.py").read_text()
    assert "overlay=0:{lay['bar_y']}" in src
    assert "Muxing stacked whitebar (bar {lay['bar_h']}px at y=0" not in src
