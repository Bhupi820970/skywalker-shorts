"""riyio look/energy port (BiBi Nov 24 2026): natural grade, exposure guard, soft drop flash, drop SFX,
speech-free sound energy + static-shot penalty. Everything must be fail-safe for the live pipeline."""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sky_fx  # noqa: E402

FF = shutil.which("ffmpeg") is not None
OLD_WHITEBAR = "eq=saturation=0.72:contrast=1.12:brightness=-0.04:gamma=0.98,curves=all='0/0 0.12/0.08 0.50/0.47 1/0.96'"
SCENES = {"night": "hue=s=0.35,format=rgb24,colorlevels=romax=0.3:gomax=0.3:bomax=0.3",
          "dim": "hue=s=0.35,format=rgb24,colorlevels=romax=0.55:gomax=0.55:bomax=0.55", "day": "hue=s=0.35"}


def _mean(vf, scene):
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=0.2", "-frames:v", "1",
                        "-vf", f"format=yuvj420p,{SCENES[scene]},{vf},format=yuvj420p,scale=64:36,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, check=True)
    return sum(r.stdout) / len(r.stdout)


def test_grade_has_no_darkening_filters():
    g = sky_fx.GRADE
    assert "contrast=" not in g and "gamma=" not in g and "brightness=-" not in g and "vignette" not in g
    assert "0.12/0.08" not in g and "noise=alls=4" in g
    assert sky_fx.FILL_BRIGHTNESS >= -0.06 and sky_fx.LUMA_MAX_DROP <= 0.08


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_grade_keeps_exposure_old_one_failed():
    for sc in SCENES:
        assert abs(_mean(sky_fx.GRADE, sc) / _mean("null", sc) - 1) <= 0.05, sc
    assert sky_fx.luma_drop([_mean(OLD_WHITEBAR, "night")], [_mean("null", "night")]) > sky_fx.LUMA_MAX_DROP


def test_pipeline_uses_new_grade_for_every_template():
    for mod in ("cv2", "librosa", "scipy"):
        pytest.importorskip(mod)                     # CI's light env has no cv2/librosa: source check below covers it
    import pipeline
    for t in ("whitebar", "depthtext", "postcard"):
        assert pipeline.cinematic_grade(t) == sky_fx.GRADE
    assert pipeline._fill_brightness() == sky_fx.FILL_BRIGHTNESS
    assert "contrast=1.12" in pipeline._legacy_grade("whitebar")      # old look kept only as fallback


def test_exposure_check():
    assert sky_fx.luma_drop([90, 45, 100], [100, 50, 100]) == pytest.approx(0.1)
    assert sky_fx.exposure_check([(60, 100)] * 5)["ok"] is False
    assert sky_fx.exposure_check([(97, 100)] * 5)["ok"] is True
    assert sky_fx.exposure_check([(10, 100)] * 2)["ok"] is True                # too few samples -> skip
    src = (ROOT / "pipeline.py").read_text()
    assert 'checks["exposure"] = sky_fx.exposure_check(pairs)' in src


def test_flash_filter_soft_two_frames():
    f = sky_fx.flash_filter(2.0, 30)
    assert f.count("drawbox") == 2 and "white@0.6" in f and "white@0.3" in f
    assert "eq(n,60)" in f and "eq(n,61)" in f and "white@1" not in f
    assert sky_fx.FLASH_OPACITY == 0.6 and sky_fx.FLASH_FRAMES == 2


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_drop_fx_on_tiny_clip(tmp_path):
    clip = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=30:d=2",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    out = sky_fx.apply_drop_video_fx(clip, 1.0, 30)
    assert out != clip
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", "format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    n = 64 * 36
    fr = [np.frombuffer(raw[i * n:(i + 1) * n], np.uint8).mean() for i in range(len(raw) // n)]
    base = fr[10]
    assert fr[30] > base + 40 and base + 10 < fr[31] < fr[30] and abs(fr[32] - base) < 3 and abs(fr[29] - base) < 3
    assert max(fr) < 235                                                          # never a flat white frame


def test_fail_safe_returns_input(tmp_path):
    missing = tmp_path / "nope.mp4"
    assert sky_fx.apply_drop_video_fx(missing, 1.0, 30) == missing
    assert sky_fx.apply_drop_video_fx(missing, None, 30) == missing
    assert sky_fx.apply_drop_sfx(missing, None, tmp_path) == missing
    assert sky_fx.apply_drop_sfx(missing, 1.0, tmp_path) == missing
    assert sky_fx.apply_sound_energy([{"start": 0, "end": 1, "motion": 3}], tmp_path / "x.mp4") == 0


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_drop_sfx_mix(tmp_path):
    a = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3",
                    str(a)], check=True)
    out = sky_fx.apply_drop_sfx(a, 1.5, tmp_path)
    assert out != a and out.exists()
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    x = np.abs(np.frombuffer(raw, np.int16).astype(float))
    assert x[: int(0.9 * 8000)].max() < 50 and x[int(1.5 * 8000): int(1.8 * 8000)].max() > 2000   # hit on the drop
    assert "adelay=1500|1500" in sky_fx.sfx_mix_filter(1.5)


def _noise(rng, n, sr, lo, hi, amp):
    f = np.fft.rfft(rng.standard_normal(n))
    fr = np.fft.rfftfreq(n, 1 / sr)
    f[(fr < lo) | (fr > hi)] = 0
    y = np.fft.irfft(f, n)
    return (amp * y / (np.abs(y).max() + 1e-9)).astype(np.float32)


def test_speech_ignored_effects_count():
    sr = sky_fx.AUDIO_SR
    rng = np.random.default_rng(2)
    sig = _noise(rng, sr * 12, sr, 20, 7000, 0.01)
    t = np.arange(sr * 4) / sr
    speech = _noise(rng, sr * 4, sr, 350, 3000, 1.0) * (0.55 + 0.45 * np.sin(2 * np.pi * 4.5 * t)).astype(np.float32)
    sig[sr * 2: sr * 6] += speech
    boom = _noise(rng, sr // 2, sr, 30, 100, 0.2) + _noise(rng, sr // 2, sr, 4500, 7500, 0.12)
    sig[sr * 9: sr * 9 + sr // 2] += boom
    sc = sky_fx.loudness_scores(sig, sr, [(2.5, 5.5), (8.9, 9.6), (10.5, 11.5)])
    assert sc[0] <= 2.0 and sc[1] >= 8.0, sc


def test_sound_energy_folds_into_spectacle_and_penalises_static():
    sr = sky_fx.AUDIO_SR
    rng = np.random.default_rng(3)
    sig = _noise(rng, sr * 10, sr, 20, 7000, 0.01)
    sig[sr * 6: sr * 7] += _noise(rng, sr, sr, 30, 90, 0.8)
    scenes = [{"start": 1.0, "end": 3.0, "motion": 5.0, "spectacle": 50.0, "score": 50.0},
              {"start": 5.8, "end": 7.2, "motion": 5.0, "spectacle": 45.0, "score": 45.0},
              {"start": 8.0, "end": 9.5, "motion": 0.4, "spectacle": 52.0, "score": 52.0},
              {"start": 0.0, "end": 0.4, "skip": True, "spectacle": 0.0}]
    assert sky_fx.apply_sound_energy(scenes, None, samples=sig) == 3
    best = max(scenes[:3], key=lambda s: s["spectacle"])
    assert best is scenes[1] and scenes[1]["loud"] >= 9                         # the impact now wins the drop
    assert scenes[2]["energy_bonus"] <= -sky_fx.STATIC_PENALTY + sky_fx.LOUD_W * scenes[2]["loud"] + 1e-6
    assert "loud" not in scenes[3]


def test_pipeline_wiring():
    src = (ROOT / "pipeline.py").read_text()
    assert "sky_fx.apply_sound_energy(scenes, src)" in src
    assert "sky_fx.apply_plate_fx(silent, DROP_T, FPS, mplan)" in src  # drop flash + motion in one pass
    assert "sky_fx.apply_drop_sfx(Path(audio), DROP_T, WORK" in src
    assert "return sky_fx.GRADE" in src and "eq=brightness={_fill_brightness()}" in src
