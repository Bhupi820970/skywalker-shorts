"""Cover-frame scoring and opening-poster mux (no pipeline, no librosa)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cover


def _solid(color, w=240, h=320) -> np.ndarray:
    fr = np.zeros((h, w, 3), dtype=np.uint8)
    fr[:] = color
    return fr


def _checker(w=240, h=320, size=8) -> np.ndarray:
    """Sharp high-contrast frame."""
    yy, xx = np.indices((h, w))
    fr = np.zeros((h, w, 3), dtype=np.uint8)
    mask = ((xx // size) + (yy // size)) % 2 == 0
    fr[mask] = (240, 240, 240)
    fr[~mask] = (20, 20, 20)
    # a warm "face-like" oval in the center
    cy, cx, ry, rx = h // 2, w // 2, 40, 32
    yy2 = (yy - cy) / ry
    xx2 = (xx - cx) / rx
    oval = yy2 * yy2 + xx2 * xx2 <= 1.0
    fr[oval] = (210, 150, 120)
    return fr


def _blurry(w=240, h=320) -> np.ndarray:
    fr = _checker(w, h).astype(np.float32)
    # box blur
    k = 21
    pad = k // 2
    out = fr.copy()
    acc = np.pad(fr, ((pad, pad), (pad, pad), (0, 0)), mode="edge")
    for c in range(3):
        # cheap separable-ish average
        s = np.cumsum(acc[..., c], axis=0)
        s = np.cumsum(s, axis=1)
        # fallback: just downsample feeling via uniform filter-like mean of neighbors
    # simpler: repeated 3x3 mean
    img = fr.copy()
    for _ in range(8):
        p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
        img = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + p[1:-1, 1:-1]) / 5.0
    return img.astype(np.uint8)


def test_score_prefers_sharp_high_contrast_over_dark_or_blurry():
    sharp = _checker()
    dark = _solid((4, 4, 4))
    blur = _blurry()
    assert cover.score_frame(sharp) > cover.score_frame(dark)
    assert cover.score_frame(sharp) > cover.score_frame(blur)
    assert cover.is_unusable_frame(dark)
    assert not cover.is_unusable_frame(sharp)


def _write_color_video(path: Path, frames: list[np.ndarray], fps: int = 10) -> Path:
    # frames are 240x320 rgb; scale to 1080x1920 later via cover helpers
    raw = path.with_suffix(".raw")
    h, w = frames[0].shape[:2]
    raw.write_bytes(b"".join(f.tobytes() for f in frames))
    wav = path.with_suffix(".wav")
    # silent mono audio matching duration
    dur = len(frames) / fps
    subprocess.check_call(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}",
         "-c:a", "aac", str(wav.with_suffix(".m4a"))],
    )
    audio = wav.with_suffix(".m4a")
    subprocess.check_call(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps),
         "-i", str(raw), "-i", str(audio),
         "-vf", f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
    )
    raw.unlink(missing_ok=True)
    audio.unlink(missing_ok=True)
    return path


def test_pick_best_frame_skips_dark_picks_sharp(tmp_path):
    dark = _solid((2, 2, 2))
    sharp = _checker()
    vid = _write_color_video(tmp_path / "cand.mp4", [dark, dark, sharp, dark], fps=4)
    picked = cover.pick_best_frame(vid, times=[0.1, 0.5, 0.8])
    assert picked is not None
    t, fr = picked
    assert cover.score_frame(fr) > 0
    # The sharp frame is the third of four at 4fps -> around 0.5-0.75s
    assert 0.4 <= t <= 1.0


def test_prepend_cover_makes_first_frame_the_poster(tmp_path):
    dark = _solid((2, 2, 2))
    sharp = _checker()
    vid = _write_color_video(tmp_path / "src.mp4", [dark, dark, dark, dark, dark, dark], fps=10)
    cover_path = cover.write_cover(
        # already 1080-ish after write_cover pads
        np.array(np.kron(sharp, np.ones((4, 4, 1)))[:1920, :1080].astype(np.uint8))
        if False else sharp,
        tmp_path / "poster.jpg",
    )
    out = cover.prepend_cover(vid, cover_path, tmp_path / "out.mp4", hold_s=cover.HOLD_S, max_total=32.6)
    assert out.exists() and out.stat().st_size > 1000
    first = cover._ffmpeg_frame(out, 0.0)
    assert first is not None
    # First frame should not be near-black like the source.
    assert float(first.mean()) > 30
    # Duration grew by the short flash only (~67ms), not a 0.2s hold.
    dur = cover._probe_dur(out)
    assert 0.62 <= dur <= 0.85
    assert cover.HOLD_S <= 0.08 and cover.HOLD_S >= 0.05


def test_candidate_times_use_edl_mids_and_skip_end_card():
    times = cover.candidate_times(20.0, edl=[
        {"t0": 0.0, "t1": 3.0, "visible": 3.0, "scene": 1},
        {"t0": 3.0, "t1": 8.0, "visible": 5.0, "scene": 2},
        {"t0": 19.0, "t1": 20.0, "visible": 1.0, "scene": "end"},
    ])
    assert any(abs(t - 1.5) < 0.2 for t in times)
    assert any(abs(t - 5.5) < 0.3 for t in times)
    assert all(t < 19.0 for t in times)


def test_hold_is_a_short_flash_not_a_fifth_second():
    assert 0.05 <= cover.HOLD_S <= 0.08
    assert round(cover.HOLD_S * cover.FPS) in (2, 3)


def test_youtube_id_and_maxres_url():
    url = "https://www.youtube.com/watch?v=Ja1q9DnLI6M"
    assert cover.youtube_id(url) == "Ja1q9DnLI6M"
    assert cover.youtube_id("https://youtu.be/Ja1q9DnLI6M") == "Ja1q9DnLI6M"
    urls = cover.official_thumb_urls(url)
    assert urls[0].endswith("/maxresdefault.jpg")
    assert "Ja1q9DnLI6M" in urls[0]


def test_official_thumb_beats_dark_frame(tmp_path):
    dark = _solid((2, 2, 2))
    sharp = _checker()
    vid = _write_color_video(tmp_path / "dark.mp4", [dark, dark, dark, dark], fps=10)
    thumb = cover.write_cover(sharp, tmp_path / "yt.jpg")
    picked = cover.pick_best_cover(vid, times=[0.1, 0.2], source_url="", work=tmp_path, thumb_path=thumb)
    assert picked is not None
    src, t, fr = picked
    assert src == "thumbnail"
    assert not cover.is_sexual_frame(fr)
    assert cover.score_frame(fr) > cover.score_frame(dark)


def test_sexual_skin_frame_is_rejected():
    # Near-full-frame skin tone, no face structure — must not become the poster.
    bare = _solid((210, 150, 120))
    assert cover.is_sexual_frame(bare)
    assert cover.score_frame(bare) < 0
    sharp = _checker()
    assert not cover.is_sexual_frame(sharp)
