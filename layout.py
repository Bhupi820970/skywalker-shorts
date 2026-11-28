"""White-hook-bar geometry for the 1080x1920 master (published at 2160x3840).

The uncropped plate is scaled to fit under the hook bar, then vertically
centered in the full frame. The white hook bar is flush with the top edge of
that plate (no gap, no overlap, not stuck at y=0 when there is room above).
Leftover space above the bar and below the plate is a blurred copy of the
footage, never a plain grey or solid white band. Never crop the picture.
"""
from __future__ import annotations


W, H = 1080, 1920


def even(n: int) -> int:
    return int(n) - (int(n) % 2)


def whitebar_layout(plate_h: int, bar_h: int, plate_w: int | None = None) -> dict:
    """Uncropped plate centered in the frame; white hook bar flush to its top edge."""
    pw0 = int(plate_w or W)
    ph0 = max(2, int(plate_h))
    bar_h = int(bar_h)
    if bar_h % 2:
        bar_h += 1
    if bar_h < 0:
        bar_h = 0
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


def bar_touches_plate(lay: dict) -> bool:
    """True when the white hook bar shares an edge with the plate (no gap)."""
    try:
        return int(lay["bar_y"]) + int(lay["bar_h"]) == int(lay["plate_y"])
    except (KeyError, TypeError, ValueError):
        return False


def geometry_ok(lay: dict) -> bool:
    """Uncropped plate is in frame, centered when it fits, hook bar flush to it.

    The Interstellar-style failure (bar stuck at the top of the 9:16 canvas
    with a blur gap before the picture) must not pass.
    """
    try:
        bar_y, bar_h = int(lay["bar_y"]), int(lay["bar_h"])
        plate_y = int(lay.get("plate_y", lay.get("video_y")))
        pw, ph = int(lay["plate_w"]), int(lay["plate_h"])
        px = int(lay.get("plate_x") or 0)
    except (KeyError, TypeError, ValueError):
        return False
    if bar_h < 2 or ph < 2 or pw < 2 or bar_y < 0 or px < 0:
        return False
    if px + pw > W or plate_y + ph > H:
        return False
    if bar_y + bar_h != plate_y:
        return False
    ideal = even((H - ph) // 2)
    room = ideal >= bar_h and ideal + ph <= H
    if room and abs(plate_y - ideal) > 2:
        return False
    return True


def compose_whitebar_frame(plate, bar, *, brightness: float = -0.14,
                           saturation: float = 0.80, contrast: float = 0.95,
                           blur_radius: float = 36):
    """One 1080x1920 frame: blur-fill canvas, uncropped plate, hook bar flush on top.

    Matches the mux look (David / cyberpsychosis Short): blurred footage above
    the bar and below the plate, a solid white hook bar touching the picture,
    the picture itself never cropped or covered by the bar.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    plate = plate.convert("RGB")
    bar = bar.convert("RGB")
    lay = whitebar_layout(plate.size[1], bar.size[1], plate.size[0])
    pw0, ph0 = plate.size
    cover = max(W / float(max(pw0, 1)), H / float(max(ph0, 1)))
    bw = max(W, int(round(pw0 * cover)))
    bh = max(H, int(round(ph0 * cover)))
    bg = plate.resize((bw, bh), Image.Resampling.LANCZOS)
    left = max(0, (bw - W) // 2)
    top = max(0, (bh - H) // 2)
    bg = bg.crop((left, top, left + W, top + H))
    if bg.size != (W, H):
        bg = bg.resize((W, H), Image.Resampling.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=float(blur_radius)))
    # ffmpeg eq=brightness: add this fraction of full scale, then sat/contrast.
    if brightness:
        arr_shift = int(round(float(brightness) * 255))
        if arr_shift:
            from PIL import ImageChops, Image as _Im
            if arr_shift < 0:
                bg = ImageChops.subtract(bg, _Im.new("RGB", bg.size, (-arr_shift,) * 3))
            else:
                bg = ImageChops.add(bg, _Im.new("RGB", bg.size, (arr_shift,) * 3))
    if saturation != 1.0:
        bg = ImageEnhance.Color(bg).enhance(float(saturation))
    if contrast != 1.0:
        bg = ImageEnhance.Contrast(bg).enhance(float(contrast))
    canvas = bg.convert("RGB")
    plate_f = plate.resize((lay["plate_w"], lay["plate_h"]), Image.Resampling.LANCZOS)
    canvas.paste(plate_f, (lay["plate_x"], lay["plate_y"]))
    bar_f = bar.resize((W, lay["bar_h"]), Image.Resampling.LANCZOS)
    canvas.paste(bar_f, (0, lay["bar_y"]))
    return canvas, lay

