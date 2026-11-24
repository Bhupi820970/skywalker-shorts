"""RIYIO-style look + energy upgrades ported to Skywalker (BiBi, Nov 24 2026).

Every public helper here is fail-safe: on ANY error it returns the input unchanged / a neutral value, so a live
render never fails because of these extras (it falls back to the old behaviour).

- GRADE: bright natural grade (no eq contrast / negative brightness / gamma, no crushed blacks, light grain).
- sound energy: music / effects loudness of the source audio with speech-dominated windows ignored, plus a soft
  penalty for static shots (motion floor) -> folded into each scene's `spectacle`, so apply_energy keeps putting
  the strongest shot on the drop.
- drop fx (energy edits, DROP_T set): soft 60% white flash for 2 frames + light RGB split (0.12 s) on the plate
  only (the white hook bar is never touched), and a generated hit (+ short whoosh into the drop) in the song mix.
- exposure guard: final plate luma vs the same mezzanine frames; QA fails if > LUMA_MAX_DROP darker.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- grade
GRADE = ("eq=saturation=1.1,"
         "curves=master='0/0.01 0.1/0.105 0.3/0.31 0.6/0.62 0.85/0.88 1/1',"
         "colorbalance=rs=-0.02:bs=0.03:rh=0.02:bh=-0.01,"
         "noise=alls=4:allf=t")
FILL_BRIGHTNESS = -0.05        # blurred cover above / below the plate (was -0.14)
LUMA_MAX_DROP = 0.08

# ---------------------------------------------------------------- drop fx
FLASH_OPACITY = 0.6
FLASH_FRAMES = 2
RGB_SPLIT_S = 0.12
RGB_SPLIT = "rgbashift=rh=-4:bh=4:rv=1:bv=-1"
HIT_GAIN, WHOOSH_GAIN = 0.55, 0.30

# ---------------------------------------------------------------- sound energy
AUDIO_SR = 16000
SPEECH_SHARE = 0.72
LOW_HZ, HIGH_HZ = 110.0, 4000.0
LOUD_W = 1.5                   # spectacle bonus per loudness point (0-10 -> up to +15)
MIN_MOTION = 1.5               # mean RGB frame difference of the sampled frames; below = static shot
STATIC_PENALTY = 12.0


def flash_filter(drop_t: float, fps: int, opacity: float = FLASH_OPACITY, frames: int = FLASH_FRAMES) -> str:
    """drawbox chain on frame numbers: flash frame k gets opacity * (1 - k / frames) white (60% then 30%)."""
    n0 = int(round(float(drop_t) * fps))
    parts = []
    for k in range(frames):
        a = round(opacity * (1 - k / frames), 3)
        parts.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{a}:t=fill:enable='eq(n,{n0 + k})'")
    return ",".join(parts)


def rgb_split_filter(drop_t: float, dur: float = RGB_SPLIT_S) -> str:
    return f"{RGB_SPLIT}:enable='between(t,{drop_t:.4f},{drop_t + dur:.4f})'"


def drop_video_filter(drop_t: float, fps: int) -> str:
    return f"{rgb_split_filter(drop_t)},{flash_filter(drop_t, fps)},format=yuv420p"


def _run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[-400:])
    return r


def apply_drop_video_fx(silent: Path, drop_t, fps: int) -> Path:
    """Soft flash + RGB split at the drop on the plate-only footage. Returns the new file or `silent` on error."""
    if drop_t is None:
        return silent
    try:
        silent = Path(silent)
        out = silent.with_name(silent.stem + "_dropfx.mp4")
        _run(["ffmpeg", "-y", "-i", str(silent), "-vf", drop_video_filter(float(drop_t), fps),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps),
              "-an", "-video_track_timescale", str(fps * 256), str(out)])
        if out.exists() and out.stat().st_size > 1000:
            print(f"  drop fx: soft flash {FLASH_OPACITY:.0%}/{FLASH_FRAMES}f + RGB split @ {float(drop_t):.2f}s",
                  flush=True)
            return out
    except Exception as e:
        print(f"  drop fx skipped ({type(e).__name__}: {str(e)[:160]})", flush=True)
    return silent


def make_sfx(dir_: Path) -> tuple[Path, Path]:
    """Self-generated (no downloads, no licence issue): low boom hit + pink-noise whoosh."""
    dir_ = Path(dir_)
    dir_.mkdir(parents=True, exist_ok=True)
    hit, whoosh = dir_ / "sfx_hit.wav", dir_ / "sfx_whoosh.wav"
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
          "aevalsrc='0.9*sin(2*PI*(45+110*exp(-t*22))*t)*exp(-t*6)':s=48000:d=0.7",
          "-af", "afade=t=out:st=0.5:d=0.2", "-ac", "2", str(hit)], timeout=60)
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:r=48000:a=0.6",
          "-af", "highpass=f=700,lowpass=f=7000,afade=t=in:d=0.28:curve=exp,afade=t=out:st=0.28:d=0.17",
          "-ac", "2", str(whoosh)], timeout=60)
    return hit, whoosh


def sfx_mix_filter(drop_t: float, whoosh: bool = True) -> str:
    """[0]=song mix, [1]=hit at the drop, [2]=whoosh ending on the drop (left out when the pre-drop silence is
    used: that half second must stay silent)."""
    hd = max(0, int(round(drop_t * 1000)))
    wd = max(0, int(round((drop_t - 0.42) * 1000)))
    if not whoosh:
        return (f"[1:a]volume={HIT_GAIN},adelay={hd}|{hd}[h];"
                f"[0:a][h]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    return (f"[1:a]volume={HIT_GAIN},adelay={hd}|{hd}[h];"
            f"[2:a]volume={WHOOSH_GAIN},adelay={wd}|{wd}[w];"
            f"[0:a][h][w]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95[a]")


def apply_drop_sfx(audio: Path, drop_t, work: Path, whoosh: bool = True) -> Path:
    """Hit + whoosh on the drop mixed into the final song bed. Returns `audio` unchanged on error."""
    if drop_t is None:
        return audio
    try:
        audio = Path(audio)
        hit, whoosh = make_sfx(Path(work) / "sfx")
        out = audio.with_name(audio.stem + "_sfx.wav")
        _run(["ffmpeg", "-y", "-i", str(audio), "-i", str(hit), "-i", str(whoosh),
              "-filter_complex", sfx_mix_filter(float(drop_t), whoosh), "-map", "[a]", "-ar", "48000", "-ac", "2",
              str(out)], timeout=180)
        if out.exists() and out.stat().st_size > 1000:
            print(f"  drop sfx: hit{' + whoosh' if whoosh else ''} @ {float(drop_t):.2f}s", flush=True)
            return out
    except Exception as e:
        print(f"  drop sfx skipped ({type(e).__name__}: {str(e)[:160]})", flush=True)
    return audio


# ---------------------------------------------------------------- sound energy (speech ignored)
def band_profile(samples: np.ndarray, sr: int, win: float = 0.1) -> tuple[np.ndarray, np.ndarray]:
    """Per 100 ms window: (music / effects energy = sub-bass + high band RMS, voice-band share 0-1)."""
    n = int(sr * win)
    k = len(samples) // n
    if k < 2:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    low, high, voice = fr < LOW_HZ, fr > HIGH_HZ, (fr >= 300) & (fr <= 3400)
    fx, share = np.zeros(k, np.float32), np.zeros(k, np.float32)
    w = np.hanning(n).astype(np.float32)
    for a in range(0, k, 1000):
        b = min(k, a + 1000)
        spec = np.abs(np.fft.rfft(samples[a * n: b * n].reshape(b - a, n) * w, axis=1)) ** 2
        tot = spec.sum(axis=1) + 1e-12
        fx[a:b] = np.sqrt((spec[:, low].sum(axis=1) + spec[:, high].sum(axis=1)) / n)
        share[a:b] = spec[:, voice].sum(axis=1) / tot
    return fx, share


def loudness_scores(samples: np.ndarray, sr: int, spans: list[tuple[float, float]], t0: float = 0.0,
                    win: float = 0.1) -> list[float]:
    """0-10 per span: 90th percentile of the non-speech energy, scaled against the whole source."""
    if samples is None or len(samples) < sr * win * 2:
        return [0.0] * len(spans)
    fx, share = band_profile(samples, sr, win)
    k = len(fx)
    fx = np.where(share > SPEECH_SHARE, 0.0, fx)
    lo, hi = float(np.percentile(fx, 10)), float(np.percentile(fx, 99))
    if hi - lo < 1e-9:
        return [0.0] * len(spans)
    out = []
    for s, e in spans:
        a = max(0, int((s - t0) / win))
        b = min(k, max(a + 1, int((e - t0) / win)))
        seg = fx[a:b]
        v = float(np.percentile(seg, 90)) if len(seg) else lo
        out.append(round(float(np.clip((v - lo) / (hi - lo), 0.0, 1.0)) * 10.0, 2))
    return out


def read_audio(path: Path, max_s: float = 600.0, sr: int = AUDIO_SR) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{max_s:.1f}", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    return np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768.0


def energy_bonus(loud: float, motion: float) -> float:
    """Spectacle delta: + loudness (speech already ignored), - penalty for a static shot."""
    return LOUD_W * float(loud) - (STATIC_PENALTY if float(motion) < MIN_MOTION else 0.0)


def apply_sound_energy(scenes: list[dict], src: Path | None, samples: np.ndarray | None = None) -> int:
    """Adds `loud` and folds energy_bonus into spectacle/score of usable scenes. Returns scenes changed (0 on error)."""
    try:
        if samples is None:
            if not src or not Path(src).exists():
                return 0
            samples = read_audio(Path(src))
        use = [s for s in scenes if not s.get("skip")]
        if not use:
            return 0
        louds = loudness_scores(samples, AUDIO_SR, [(float(s["start"]), float(s["end"])) for s in use])
        for s, ld in zip(use, louds):
            d = energy_bonus(ld, float(s.get("motion") or 0.0))
            s["loud"] = ld
            s["energy_bonus"] = round(d, 2)
            s["spectacle"] = round(float(s.get("spectacle") or 0.0) + d, 3)
            s["score"] = round(float(s.get("score") or 0.0) + d, 3)
        print(f"  sound energy: {len(use)} scenes scored (speech ignored), "
              f"{sum(1 for s in use if float(s.get('motion') or 0) < MIN_MOTION)} static shot(s) penalised",
              flush=True)
        return len(use)
    except Exception as e:
        print(f"  sound energy skipped ({type(e).__name__}: {str(e)[:160]})", flush=True)
        return 0


# ---------------------------------------------------------------- exposure guard
def luma_drop(final_y: list[float], source_y: list[float]) -> float:
    """Median relative darkening of matching samples (0.1 = 10% darker; negative = brighter)."""
    r = [f / s for f, s in zip(final_y, source_y) if s and s > 4.0]
    if not r:
        return 0.0
    return round(1.0 - float(np.median(r)), 4)


def exposure_check(pairs: list[tuple[float, float]], min_samples: int = 4) -> dict:
    """pairs = [(final plate luma, source luma)]. ok unless clearly darker than the source."""
    if len(pairs) < min_samples:
        return {"ok": True, "skipped": f"only {len(pairs)} samples", "pairs": pairs}
    d = luma_drop([a for a, _ in pairs], [b for _, b in pairs])
    return {"ok": d <= LUMA_MAX_DROP, "luma_drop": d, "limit": LUMA_MAX_DROP, "pairs": pairs[:10]}


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------- motion (ref1/ref2: the screen is never still)
# BiBi Nov 24 ("skywalker ko riyo ki trh"): slow 100->105% push-in on dialogue / static / long clips, and on montage
# beats after the drop a quick zoom punch (1.0 -> 1.08 -> 1.0) with a light shake and a 2-frame RGB split.
# One extra plate pass (same pass as the drop flash), plate only: the white hook bar is never touched.
SLOW_ZOOM = 0.05
SLOW_ZOOM_MIN_S = 1.0
PUNCH_Z = 0.08
PUNCH_S = 0.24
PUNCH_MIN_GAP = 0.45
PUNCH_MAX = 24
SHAKE_PX = 5.0
PUNCH_RGB_S = 0.07
PUNCH_RGB = "rgbashift=rh=-3:bh=3"


def motion_plan(edl: list[dict], drop_t=None, beats_out=None, punches: bool = True) -> dict:
    """{'zooms': [(t0, t1)], 'punches': [t]} on the output timeline.
    zooms: every clip >= 1 s before the drop (dialogue / build-up), every act-1 clip and every long (>= 1.5 s,
    hero / static) clip after it. punches: cuts after the drop (they are beat-snapped) + strong beats inside long
    montage shots, at least 0.45 s apart, at most 24 - never every frame."""
    items = [it for it in (edl or []) if it.get("scene") != "end"]
    zooms, cuts = [], []
    d = None if drop_t is None else float(drop_t)
    for it in items:
        t0, t1 = float(it["t0"]), float(it["t1"])
        if t1 - t0 >= SLOW_ZOOM_MIN_S and (d is None or t0 < d - 0.05 or it.get("act") == 1 or t1 - t0 >= 1.5):
            zooms.append((round(t0, 3), round(t1, 3)))
        if d is not None and t0 >= d - 0.02:
            cuts.append(t0)
    pts: list[float] = []
    if punches and d is not None:
        cut_set = set([round(d, 3)] + [round(t, 3) for t in cuts])
        cand = sorted(cut_set | {round(float(b), 3) for b in (beats_out or []) if float(b) > d})
        end = max([float(it["t1"]) for it in items] or [0.0])
        for t in cand:
            if t > end - PUNCH_S:
                continue
            gap = PUNCH_MIN_GAP if t in cut_set else 2 * PUNCH_MIN_GAP   # beats inside a shot: every other beat
            if pts and t - pts[-1] < gap:
                continue
            if t not in cut_set and any(abs(t - c) < gap for c in cut_set):
                continue
            pts.append(t)
            if len(pts) >= PUNCH_MAX:
                break
    return {"zooms": zooms, "punches": pts}


def _zoom_expr(plan: dict) -> str:
    parts = [f"between(t\\,{a:.3f}\\,{b:.3f})*{SLOW_ZOOM}*(t-{a:.3f})/{max(0.05, b - a):.3f}"
             for a, b in plan.get("zooms") or []]
    parts += [f"between(t\\,{p:.3f}\\,{p + PUNCH_S:.3f})*{PUNCH_Z}*sin(PI*(t-{p:.3f})/{PUNCH_S})"
              for p in plan.get("punches") or []]
    return "1" + "".join("+" + x for x in parts)


def _shake_expr(plan: dict, axis: str) -> str:
    f, ph = (23, "sin") if axis == "x" else (17, "cos")
    parts = [f"between(t\\,{p:.3f}\\,{p + PUNCH_S:.3f})*{SHAKE_PX}*{ph}(2*PI*{f}*(t-{p:.3f}))*(1-(t-{p:.3f})/{PUNCH_S})"
             for p in plan.get("punches") or []]
    return "+".join(parts) if parts else "0"


def zoom_at(t: float, plan: dict | None) -> float:
    """Python twin of the ffmpeg zoom expression (QA compares the source frame zoomed the same way)."""
    if not plan:
        return 1.0
    z = 1.0
    for a, b in plan.get("zooms") or []:
        if a <= t <= b:
            z += SLOW_ZOOM * (t - a) / max(0.05, b - a)
    for p in plan.get("punches") or []:
        if p <= t <= p + PUNCH_S:
            z += PUNCH_Z * float(np.sin(np.pi * (t - p) / PUNCH_S))
    return z


def in_punch(t: float, plan: dict | None, pad: float = 0.08) -> bool:
    return any(p - pad <= t <= p + PUNCH_S + pad for p in (plan or {}).get("punches") or [])


def motion_filter(plan: dict, w: int, h: int) -> str:
    """scale (per-frame zoom) + centred crop back to w x h (+ shake) + 2-frame RGB split on each punch."""
    z = _zoom_expr(plan)
    f = (f"scale=w='2*trunc({w}*({z})/2)':h='2*trunc({h}*({z})/2)':eval=frame:flags=bicubic,"
         f"crop={w}:{h}:x='clip((in_w-{w})/2+({_shake_expr(plan, 'x')})\\,0\\,in_w-{w})'"
         f":y='clip((in_h-{h})/2+({_shake_expr(plan, 'y')})\\,0\\,in_h-{h})',setsar=1")
    pp = plan.get("punches") or []
    if pp:
        en = "+".join(f"between(t\\,{p:.3f}\\,{p + PUNCH_RGB_S:.3f})" for p in pp)
        f += f",{PUNCH_RGB}:enable='{en}'"
    return f


def _probe_wh(path: Path) -> tuple[int, int]:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
              "-of", "csv=p=0", str(path)], timeout=60)
    w, h = [int(x) for x in r.stdout.strip().split(",")[:2]]
    return w, h


def apply_plate_fx(silent: Path, drop_t, fps: int, plan: dict | None = None) -> Path:
    """ONE pass over the plate: motion (slow zoom + beat punches) then the soft drop flash / RGB split.
    Falls back to the drop-only pass, then to the untouched plate, on any error."""
    if not plan or not (plan.get("zooms") or plan.get("punches")):
        return apply_drop_video_fx(silent, drop_t, fps)
    try:
        silent = Path(silent)
        w, h = _probe_wh(silent)
        vf = motion_filter(plan, w, h)
        if drop_t is not None:
            vf += "," + drop_video_filter(float(drop_t), fps)
        else:
            vf += ",format=yuv420p"
        out = silent.with_name(silent.stem + "_fx.mp4")
        _run(["ffmpeg", "-y", "-i", str(silent), "-vf", vf,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps),
              "-an", "-video_track_timescale", str(fps * 256), str(out)], timeout=1200)
        if out.exists() and out.stat().st_size > 1000:
            print(f"  plate fx: slow zoom on {len(plan.get('zooms') or [])} clip(s), "
                  f"{len(plan.get('punches') or [])} beat punch(es)"
                  + (f", drop flash @ {float(drop_t):.2f}s" if drop_t is not None else ""), flush=True)
            return out
    except Exception as e:
        print(f"  plate motion fx skipped ({type(e).__name__}: {str(e)[:160]}); drop fx only", flush=True)
    return apply_drop_video_fx(silent, drop_t, fps)


# ---------------------------------------------------------------- pre-drop silence + sub-bass riser
PRE_DROP_SILENCE = 0.5
RISER_S = 1.6
RISER_GAIN = 0.32
MIN_SILENCE = 0.15


def _read_f32(path: Path, sr: int = 48000) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-300:])
    return np.frombuffer(r.stdout, np.float32).reshape(-1, 2).copy()


def _write_f32(path: Path, x: np.ndarray, sr: int = 48000) -> None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ac", "2", "-ar", str(sr), "-i", "-",
                        "-c:a", "pcm_s16le", str(path)], input=np.ascontiguousarray(x, np.float32).tobytes(),
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-300:])


def riser(dur: float, sr: int = 48000, seed: int = 7) -> np.ndarray:
    """Generated sub-bass riser: 35 -> 95 Hz sine sweep + a soft airy noise swell, rising in level."""
    n = max(1, int(dur * sr))
    t = np.arange(n) / sr
    f = 35.0 + 60.0 * (t / dur) ** 2
    phase = 2 * np.pi * np.cumsum(f) / sr
    env = (t / dur) ** 2
    sub = np.sin(phase) * env
    rng = np.random.default_rng(seed)
    nz = rng.standard_normal(n)
    nz = nz - np.convolve(nz, np.ones(24) / 24, mode="same")      # crude high-pass: airy swell
    sw = nz * (t / dur) ** 3 * 0.18
    y = (0.85 * sub + sw).astype(np.float32)
    fo = min(n, int(0.012 * sr))
    if fo:
        y[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)
    return np.stack([y, y], axis=1) * RISER_GAIN


def pre_drop_window(drop_t: float, speech_end: float | None = None,
                    silence_s: float = PRE_DROP_SILENCE) -> tuple[float, float] | None:
    """(silence_start, drop_t). With dialogue, the silence never cuts a word: it starts after the last word."""
    ss = drop_t - silence_s
    if speech_end is not None:
        ss = max(ss, float(speech_end) + 0.04)
    if drop_t - ss < MIN_SILENCE or ss < RISER_S * 0.5:
        return None
    return round(ss, 3), round(float(drop_t), 3)


def apply_pre_drop_audio(audio: Path, drop_t, work: Path, speech_end: float | None = None) -> Path:
    """Riser into ~0.5 s of full silence right before the drop. Returns `audio` unchanged on error / no drop."""
    if drop_t is None:
        return audio
    try:
        audio = Path(audio)
        win = pre_drop_window(float(drop_t), speech_end)
        if not win:
            print("  pre-drop silence skipped (no room before the drop)", flush=True)
            return audio
        ss, dt = win
        sr = 48000
        x = _read_f32(audio, sr)
        a, b = int(ss * sr), int(dt * sr)
        if b >= len(x) - 10:
            return audio
        r = riser(min(RISER_S, ss), sr)
        r0 = max(0, a - len(r))
        x[r0:a] += r[len(r) - (a - r0):]
        g = np.ones(len(x), np.float32)
        ramp = int(0.012 * sr)
        g[max(0, a - ramp):a] = np.linspace(1, 0, a - max(0, a - ramp), dtype=np.float32)
        g[a:b] = 0.0
        x *= g[:, None]
        peak = float(np.abs(x).max() or 1.0)
        if peak > 0.98:
            x *= 0.98 / peak
        out = audio.with_name(audio.stem + "_predrop.wav")
        _write_f32(out, x, sr)
        print(f"  pre-drop: sub-bass riser {min(RISER_S, ss):.1f}s + silence {ss:.2f}-{dt:.2f}s", flush=True)
        return out
    except Exception as e:
        print(f"  pre-drop silence skipped ({type(e).__name__}: {str(e)[:160]})", flush=True)
    return audio
