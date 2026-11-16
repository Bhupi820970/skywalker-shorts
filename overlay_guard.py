"""Reject footage with a burned-in logo, watermark, network bug, or hardcoded subtitle.

Samples frames and looks for a static corner overlay (low temporal variance
where the picture moves) and a persistent glyph band along the bottom.
Flat letterbox is not an overlay. Callers skip the clip; nothing is cropped.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

_LOGO_CHANNEL = re.compile(
    r"("
    r"\bcrunchyroll\b|\bfunimation\b|\bhidive\b|"
    r"\bani[\s\-]?one\b|"
    r"\bmuse\s+(?:asia|indonesia|malaysia|thailand|vietnam|philippines|brasil|latam)\b|"
    r"\bgogoanime\b|\b9anime\b|\bkickassanime\b|\banimepahe\b|\bhianime\b|"
    r"\bmovieclips\b|\bfilmibeat\b"
    r")",
    re.I,
)


def channel_blocked(name: str) -> bool:
    n = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return bool(n) and bool(_LOGO_CHANNEL.search(n))


def _shrink(fr: np.ndarray, width: int = 480) -> np.ndarray:
    h, w = fr.shape[:2]
    if w <= width:
        return fr
    nh = max(2, int(round(h * width / float(w))))
    ys = np.linspace(0, h - 1, nh).astype(np.int32)
    xs = np.linspace(0, w - 1, width).astype(np.int32)
    return fr[ys][:, xs]


def _gray(frames: list[np.ndarray]) -> np.ndarray:
    return np.stack([_shrink(f).astype(np.float32).mean(axis=2) for f in frames], axis=0)


def _grad(g: np.ndarray) -> np.ndarray:
    gx = np.abs(np.diff(g, axis=1, append=g[:, -1:]))
    gy = np.abs(np.diff(g, axis=0, append=g[-1:, :]))
    return gx + gy


def _corner_logo(std: np.ndarray, grad: np.ndarray, med: np.ndarray, center_motion: float) -> bool:
    if center_motion < 5.0:
        return False
    detail = (std < 7.5) & (grad > 20.0) & (med > 28.0)
    row_frac = detail.mean(axis=1) if detail.size else np.zeros(0)
    if row_frac.size:
        keep = row_frac <= 0.55
        if keep.any() and not keep.all():
            detail = detail[keep]
    if detail.size == 0:
        return False
    frac = float(detail.mean())
    return 0.012 <= frac <= 0.42


def _subtitle_band(std: np.ndarray, grad: np.ndarray, center_motion: float) -> bool:
    if center_motion < 5.0:
        return False
    h, w = std.shape
    y0, y1 = int(h * 0.76), int(h * 0.98)
    x0, x1 = int(w * 0.08), int(w * 0.92)
    if y1 - y0 < 6 or x1 - x0 < 16:
        return False
    detail = (std[y0:y1, x0:x1] < 7.5) & (grad[y0:y1, x0:x1] > 18.0)
    row = detail.mean(axis=1)
    text_rows = (row > 0.08) & (row < 0.55)
    longest = cur = 0
    for flag in text_rows:
        if flag:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return 2 <= longest <= max(4, int(0.40 * (y1 - y0)))


def assess_frames(frames: list[np.ndarray]) -> str | None:
    frames = [f for f in frames if f is not None and getattr(f, "size", 0)]
    if len(frames) < 2:
        return None
    if len(frames) > 8:
        idx = np.linspace(0, len(frames) - 1, 8).astype(int)
        frames = [frames[int(i)] for i in idx]
    g = _gray(frames)
    med = np.median(g, axis=0)
    std = g.std(axis=0)
    grad = _grad(med)
    h, w = med.shape
    center_motion = float(std[int(h * 0.28):int(h * 0.72), int(w * 0.28):int(w * 0.72)].mean())
    cw, ch = max(8, int(w * 0.18)), max(8, int(h * 0.18))
    corners = (
        (0, 0, ch, cw),
        (0, w - cw, ch, w),
        (h - ch, 0, h, cw),
        (h - ch, w - cw, h, w),
    )
    for y0, x0, y1, x1 in corners:
        if _corner_logo(std[y0:y1, x0:x1], grad[y0:y1, x0:x1], med[y0:y1, x0:x1], center_motion):
            return "logo/watermark"
    if _subtitle_band(std, grad, center_motion):
        return "burned-in subtitles"
    return None


def _ppm_frame(blob: bytes) -> np.ndarray | None:
    if not blob.startswith(b"P6"):
        return None
    try:
        _header, _, rest = blob.partition(b"\n")
        dims, _, rest = rest.partition(b"\n")
        while dims.startswith(b"#"):
            dims, _, rest = rest.partition(b"\n")
        maxv, _, pixels = rest.partition(b"\n")
        w_s, h_s = dims.split()
        w, h = int(w_s), int(h_s)
        if maxv.strip() != b"255":
            return None
        need = w * h * 3
        if len(pixels) < need:
            return None
        return np.frombuffer(pixels[:need], dtype=np.uint8).reshape(h, w, 3).copy()
    except Exception:
        return None


def sample_frames(path: Path, n: int = 6, width: int = 480) -> list[np.ndarray]:
    try:
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            text=True, timeout=60,
        ).strip() or 0)
    except Exception:
        dur = 0.0
    if dur <= 0.4:
        times = [0.1]
    else:
        times = [min(dur - 0.15, max(0.1, dur * f)) for f in (0.08, 0.22, 0.38, 0.54, 0.70, 0.86)[:n]]
    out: list[np.ndarray] = []
    for t in times:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={int(width)}:-2",
             "-f", "image2pipe", "-vcodec", "ppm", "pipe:1"],
            capture_output=True, timeout=90,
        )
        fr = _ppm_frame(r.stdout or b"")
        if fr is not None:
            out.append(fr)
    return out


def clip_reason(path: Path) -> str | None:
    frames = sample_frames(path)
    if len(frames) < 2:
        return "could not verify clean frames"
    return assess_frames(frames)
