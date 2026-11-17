"""Detect music beats and snap cut times to them.

Prefers librosa when it is installed (GitHub runners). Falls back to an
ffmpeg + numpy energy/onset estimator so local tests can run without librosa.
If both fail, returns an even grid at a default tempo so cuts still have a pulse.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

DEFAULT_BPM = 100.0
SR = 22050
HOP = 512


def _even_grid(duration_s: float, bpm: float = DEFAULT_BPM) -> np.ndarray:
    ibi = 60.0 / max(40.0, float(bpm))
    if duration_s <= 0:
        return np.array([0.0], dtype=float)
    return np.arange(0.0, duration_s + 1e-6, ibi)


def _ffmpeg_mono(path: Path, sr: int = SR) -> tuple[np.ndarray, int] | None:
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
            capture_output=True, check=False,
        )
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    y = np.frombuffer(r.stdout, dtype=np.float32)
    if y.size < sr // 2:
        return None
    return y, sr


def _rms_onset(y: np.ndarray, sr: int, hop: int = HOP) -> tuple[np.ndarray, np.ndarray]:
    if y.size < hop * 4:
        return np.zeros(1), np.zeros(1)
    n = 1 + (len(y) - hop) // hop
    frames = np.lib.stride_tricks.as_strided(
        y, shape=(n, hop), strides=(y.strides[0] * hop, y.strides[0])
    ).copy()
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    onset = np.maximum(0.0, np.diff(rms, prepend=rms[:1]))
    times = np.arange(len(onset)) * (hop / float(sr))
    return times, onset


def _tempo_from_onset(times: np.ndarray, onset: np.ndarray, sr: int = SR, hop: int = HOP) -> float:
    if onset.size < 16:
        return DEFAULT_BPM
    # Autocorrelate the onset envelope in a 60-180 BPM window.
    w = onset - float(np.mean(onset))
    ac = np.correlate(w, w, mode="full")[len(w) - 1:]
    min_lag = max(1, int((60.0 / 180.0) * sr / hop))
    max_lag = max(min_lag + 1, int((60.0 / 60.0) * sr / hop))
    max_lag = min(max_lag, len(ac) - 1)
    if max_lag <= min_lag:
        return DEFAULT_BPM
    region = ac[min_lag:max_lag]
    lag = int(np.argmax(region)) + min_lag
    bpm = 60.0 * sr / (lag * hop)
    if bpm >= 140:
        bpm = bpm / 2.0
    return float(np.clip(bpm, 60.0, 160.0))


def _beats_from_onset(times: np.ndarray, onset: np.ndarray, bpm: float, duration_s: float) -> np.ndarray:
    ibi = 60.0 / max(40.0, float(bpm))
    if times.size == 0:
        return _even_grid(duration_s, bpm)
    # Peak-pick: local max above a median threshold, then snap to a regular grid
    # starting at the strongest early peak.
    thr = float(np.median(onset) + 0.4 * (np.percentile(onset, 80) - np.median(onset)))
    peaks = []
    for i in range(1, len(onset) - 1):
        if onset[i] >= onset[i - 1] and onset[i] >= onset[i + 1] and onset[i] >= thr:
            peaks.append(float(times[i]))
    if not peaks:
        return _even_grid(duration_s, bpm)
    # Pick the start so later beats stay close to peaks.
    start = peaks[0]
    for p in peaks[:8]:
        if p < ibi * 2:
            start = p
            break
    beats = []
    t = start
    # also walk backward to 0
    while t - ibi >= -0.01:
        t -= ibi
    t = max(0.0, t)
    while t <= duration_s + 0.05:
        beats.append(t)
        t += ibi
    return np.array(beats, dtype=float) if beats else _even_grid(duration_s, bpm)


def _detect_ffmpeg(path: Path) -> dict:
    loaded = _ffmpeg_mono(path)
    if not loaded:
        return {"tempo": DEFAULT_BPM, "beats": [], "duration_s": 0.0, "method": "grid", "ok": False}
    y, sr = loaded
    duration_s = float(len(y) / sr)
    times, onset = _rms_onset(y, sr)
    tempo = _tempo_from_onset(times, onset, sr)
    beats = _beats_from_onset(times, onset, tempo, duration_s)
    return {
        "tempo": round(float(tempo), 2),
        "beats": [round(float(b), 4) for b in beats],
        "duration_s": round(duration_s, 3),
        "method": "ffmpeg",
        "ok": True,
    }


def _detect_librosa(path: Path) -> dict | None:
    try:
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(str(path), sr=SR, mono=True)
    except Exception:
        return None
    duration_s = float(len(y) / sr) if sr else 0.0
    if duration_s < 0.5:
        return None
    try:
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        tempo = float(tempo[0] if hasattr(tempo, "__len__") else tempo)
        pulse = np.asarray(beats, dtype=float)
        if tempo >= 140 and pulse.size > 8:
            pulse = pulse[::2]
            tempo = tempo / 2.0
        if pulse.size == 0:
            return None
        return {
            "tempo": round(float(tempo), 2),
            "beats": [round(float(b), 4) for b in pulse],
            "duration_s": round(duration_s, 3),
            "method": "librosa",
            "ok": True,
        }
    except Exception:
        return None


def detect_beats(path: str | Path, *, prefer: str | None = None) -> dict:
    """Return {tempo, beats, duration_s, method, ok}.

    `prefer` is 'librosa', 'ffmpeg', or None (try librosa then ffmpeg then grid).
    """
    path = Path(path)
    duration_s = 0.0
    order = ["librosa", "ffmpeg"] if prefer is None else [prefer]
    if prefer is None:
        pass
    for how in order:
        if how == "librosa":
            info = _detect_librosa(path)
            if info and info.get("beats"):
                return info
        elif how == "ffmpeg":
            info = _detect_ffmpeg(path)
            if info.get("ok") and info.get("beats"):
                return info
            duration_s = float(info.get("duration_s") or 0.0)
    if duration_s <= 0 and path.exists():
        try:
            o = subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                text=True,
            ).strip()
            duration_s = float(o or 0)
        except Exception:
            duration_s = 30.0
    beats = _even_grid(duration_s or 30.0, DEFAULT_BPM)
    return {
        "tempo": DEFAULT_BPM,
        "beats": [round(float(b), 4) for b in beats],
        "duration_s": round(float(duration_s or 30.0), 3),
        "method": "grid",
        "ok": False,
    }


def snap_times(times: list[float] | np.ndarray, beats: list[float] | np.ndarray,
               *, max_shift: float = 0.35) -> list[float]:
    """Move each cut time to the nearest beat, but never more than max_shift seconds."""
    beats = np.asarray(beats, dtype=float)
    out = []
    for t in list(times):
        t = float(t)
        if beats.size == 0:
            out.append(t)
            continue
        j = int(np.argmin(np.abs(beats - t)))
        snapped = float(beats[j])
        out.append(snapped if abs(snapped - t) <= max_shift else t)
    return out
