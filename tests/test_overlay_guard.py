"""Logo / watermark / burned-subtitle rejection. No video files."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import overlay_guard as og


def _moving(rng, n=5, h=270, w=480):
    frames = []
    for i in range(n):
        fr = rng.integers(20, 190, size=(h, w, 3), dtype=np.uint8)
        fr[:] = (fr.astype(np.int16) + i * 24) % 200
        frames.append(fr.astype(np.uint8))
    return frames


def test_clean_motion_passes():
    assert og.assess_frames(_moving(np.random.default_rng(1))) is None


def test_static_corner_logo_is_rejected():
    frames = _moving(np.random.default_rng(2))
    for fr in frames:
        fr[6:24, 8:110] = 250
        for x in range(12, 100, 9):
            fr[8:22, x:x + 3] = 15
    assert og.assess_frames(frames) == "logo/watermark"


def test_burned_in_subtitles_are_rejected():
    frames = _moving(np.random.default_rng(3))
    for fr in frames:
        fr[210:236, 100:380] = 12
        for x in range(108, 372, 12):
            fr[214:232, x:x + 5] = 245
    assert og.assess_frames(frames) == "burned-in subtitles"


def test_logo_channels_blocked_clean_studios_not():
    assert og.channel_blocked("Crunchyroll")
    assert og.channel_blocked("Funimation")
    assert og.channel_blocked("Ani-One Asia")
    assert og.channel_blocked("Muse Asia")
    assert not og.channel_blocked("TOHO animation")
    assert not og.channel_blocked("Netflix Anime")
