"""Cuts stay near 1.5s+, with hero shots and only sparse beat flashes."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import edit_pace


def test_pacing_constants():
    assert edit_pace.MIN_SHOT_LEN == 1.5
    assert edit_pace.HERO_SHOT_MIN == 2.0
    assert edit_pace.HERO_SHOT_MAX == 4.0
    assert edit_pace.MAX_FLASH_SHOTS == 2
    assert edit_pace.MAX_HERO_SHOTS == 2


def test_plan_edit_min_shot_and_hero():
    import pytest
    pytest.importorskip("librosa")
    pytest.importorskip("cv2")
    import numpy as np
    import pipeline
    import similarity

    def scene(i, start, dur=3.2):
        rng = np.random.default_rng(10 + i)
        fr = rng.integers(0, 255, (48, 48, 3), dtype=np.uint8)
        fr[:, :, 0] = (i * 37) % 255
        fp = similarity.fingerprint_frame(fr)
        return {
            "id": i, "start": start, "end": start + dur, "dur": dur,
            "skip": False, "score": 20.0 + i, "spectacle": 15.0 + (i % 7),
            "skin": 0.04, "motion": 12.0, "speech": 0.0, "luma": 70.0,
            "phash": fp["phash"], "hist": fp["hist"],
        }

    pipeline.RELAX = False
    pipeline.EDIT_MODE = "music_only"
    scenes = [scene(i, i * 10.0, dur=4.0) for i in range(14)]
    music = {"pulses": list(np.linspace(0, 40, 81))}
    edl = pipeline.plan_edit(scenes, music, target=24.0)
    vis = [float(it["visible"]) for it in edl]
    assert vis, "expected a cut list"
    flashes = [v for v in vis if v < pipeline.MIN_SHOT_LEN - 0.05]
    assert all(v >= pipeline.FLASH_SHOT_MIN - 0.05 for v in flashes)
    assert len(flashes) <= pipeline.MAX_FLASH_SHOTS
    regular = [v for v in vis if v >= pipeline.MIN_SHOT_LEN - 0.05]
    assert regular, f"no regular shots in {vis}"
    heroes = [v for v in vis if v >= pipeline.HERO_SHOT_MIN - 0.05]
    assert heroes, f"expected at least one 2-4s hero shot, got {vis}"
    assert max(vis) <= pipeline.HERO_SHOT_MAX + 0.1
    ids = [it["scene"] for it in edl]
    assert len(ids) == len(set(ids))
