"""Beat detection fallback (no librosa) and cut snapping."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import beat_sync


def _click_track(path: Path, bpm: float = 100.0, seconds: float = 4.0, sr: int = 22050) -> Path:
    """Write a short wav of clicks on the beat via ffmpeg (no librosa)."""
    ibi = 60.0 / bpm
    n = int(seconds * sr)
    y = np.zeros(n, dtype=np.float32)
    t = 0.0
    while t < seconds:
        i = int(t * sr)
        end = min(n, i + int(0.012 * sr))
        if i < n:
            y[i:end] = 0.9
        t += ibi
    raw = path.with_suffix(".f32")
    raw.write_bytes(y.tobytes())
    subprocess.check_call(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", str(raw),
         str(path)],
    )
    raw.unlink(missing_ok=True)
    return path


def test_ffmpeg_fallback_detects_clicks(tmp_path):
    wav = _click_track(tmp_path / "clicks.wav", bpm=100.0, seconds=4.0)
    info = beat_sync.detect_beats(wav, prefer="ffmpeg")
    assert info["ok"] is True
    assert info["method"] == "ffmpeg"
    assert 80 <= info["tempo"] <= 120
    assert len(info["beats"]) >= 4


def test_grid_fallback_when_file_missing():
    info = beat_sync.detect_beats("/tmp/definitely-missing-skywalker-beat.mp3", prefer="nope")
    assert info["method"] == "grid"
    assert info["ok"] is False
    assert len(info["beats"]) >= 4


def test_snap_times_moves_near_beats_only():
    beats = [0.0, 0.6, 1.2, 1.8]
    out = beat_sync.snap_times([0.08, 1.15, 3.0], beats, max_shift=0.35)
    assert abs(out[0] - 0.0) < 1e-6
    assert abs(out[1] - 1.2) < 1e-6
    # 3.0 is farther than max_shift from 1.8, so it stays
    assert abs(out[2] - 3.0) < 1e-6
