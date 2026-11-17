"""White-hook-bar geometry for the 1080x1920 master (published at 2160x3840).

The bar is its own band at the top and never overlaps the picture. The
uncropped plate is scaled to fit under the bar, then vertically centered in
the full frame. Leftover space above and below the plate is letterbox or blur.
"""
from __future__ import annotations


W, H = 1080, 1920


def even(n: int) -> int:
    return int(n) - (int(n) % 2)


def whitebar_layout(plate_h: int, bar_h: int, plate_w: int | None = None) -> dict:
    """Bar at y=0. Uncropped plate centered in the frame, never under the bar."""
    pw0 = int(plate_w or W)
    ph0 = max(2, int(plate_h))
    bar_h = int(bar_h)
    if bar_h % 2:
        bar_h += 1
    if bar_h < 2:
        bar_h = 2
    if bar_h > H - 4:
        bar_h = H - 4
        if bar_h % 2:
            bar_h -= 1
    region_h = H - bar_h
    scale = min(W / float(max(pw0, 1)), region_h / float(ph0))
    pw = even(max(2, int(round(pw0 * scale))))
    ph = even(max(2, int(round(ph0 * scale))))
    if pw > W:
        pw = W if W % 2 == 0 else W - 1
    if ph > region_h:
        ph = region_h if region_h % 2 == 0 else region_h - 1
    plate_x = even(max(0, (W - pw) // 2))
    if plate_x + pw > W:
        plate_x = max(0, W - pw)
    # Center in the full 1080x1920 frame when that leaves the bar clear.
    plate_y = even((H - ph) // 2)
    min_y = bar_h
    max_y = even(max(min_y, H - ph))
    if plate_y < min_y:
        plate_y = min_y
    if plate_y > max_y:
        plate_y = max_y
    # Hook bar sits directly on top of the centered plate (not stuck under the
    # phone's top UI). It never overlaps the picture.
    bar_y = even(max(0, plate_y - bar_h))
    return {
        "bar_y": bar_y,
        "bar_h": bar_h,
        "video_y": plate_y,
        "plate_y": plate_y,
        "plate_x": plate_x,
        "plate_w": pw,
        "plate_h": ph,
        "region_h": region_h,
        "block_h": bar_h + region_h,
    }
