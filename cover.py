"""Pick a sharp, high-contrast cover frame and put it at the start of the Short.

YouTube Shorts (and Buffer's YouTube path) use the first frame as the cover;
Buffer rejects a custom thumbnailUrl on video assets. So the poster has to
live in the video itself. The plate stays centered and is never cropped.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

W, H = 1080, 1920
HOLD_S = 0.20
MAX_TOTAL_S = 32.6
FPS = 30


def _luma(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3:
        a = frame.astype(np.float32)
        return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    return frame.astype(np.float32)


def sharpness(frame: np.ndarray) -> float:
    """Laplacian variance; blurry frames score near 0."""
    L = _luma(frame)
    try:
        import cv2
        return float(cv2.Laplacian(L, cv2.CV_32F, ksize=3).var())
    except Exception:
        k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
        pad = np.pad(L, 1, mode="edge")
        acc = np.zeros_like(L)
        for dy in range(3):
            for dx in range(3):
                acc += k[dy, dx] * pad[dy:dy + L.shape[0], dx:dx + L.shape[1]]
        return float(acc.var())


def contrast(frame: np.ndarray) -> float:
    return float(_luma(frame).std())


def mean_luma(frame: np.ndarray) -> float:
    return float(_luma(frame).mean())


def brightness_score(luma: float) -> float:
    """Prefer mid-bright story frames; penalise near-black and blown-out."""
    return float(max(0.0, 1.0 - abs(float(luma) - 110.0) / 110.0))


def face_bonus(frame: np.ndarray) -> float:
    try:
        import scene_filter
        return float(scene_filter.face_or_character_score(frame))
    except Exception:
        a = frame
        if a.ndim != 3 or a.shape[2] < 3:
            return 0.0
        r, g, b = a[:, :, 0].astype(int), a[:, :, 1].astype(int), a[:, :, 2].astype(int)
        skin = ((r > 80) & (r > g) & (g > b * 0.7) & ((r - b) > 15)).mean()
        return float(skin)


def is_unusable_frame(frame: np.ndarray) -> bool:
    """Dark, flat, or blurry frames never become the poster."""
    y = mean_luma(frame)
    if y < 28 or y > 230:
        return True
    if contrast(frame) < 18:
        return True
    if sharpness(frame) < 12:
        return True
    return False


def score_frame(frame: np.ndarray) -> float:
    """Higher is a better poster: sharp, contrasty, not dark, prefers a face."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return -1e9
    if is_unusable_frame(frame):
        return -1.0
    sharp = sharpness(frame)
    con = contrast(frame)
    bright = brightness_score(mean_luma(frame))
    face = face_bonus(frame)
    # Sharpness dominates so a soft pretty frame loses to a crisp close-up.
    return 0.40 * min(sharp / 400.0, 1.5) + 0.25 * min(con / 60.0, 1.5) + 0.20 * bright + 0.15 * min(face, 1.5)


def candidate_times(duration_s: float, edl: list[dict] | None = None, *,
                    end_card_s: float = 1.0) -> list[float]:
    """Mid-shot times, skipping a title-card-ish tail and the very first instant."""
    times: list[float] = []
    if edl:
        for shot in edl:
            if (shot.get("scene") in ("end", None) and shot.get("id") == "end") or shot.get("scene") == "end":
                continue
            t0 = float(shot.get("t0") or shot.get("start") or 0.0)
            vis = float(shot.get("visible") or shot.get("dur") or 0.0)
            t1 = float(shot.get("t1") or (t0 + vis) or 0.0)
            if t1 - t0 < 0.12:
                continue
            mid = t0 + max(0.08, 0.5 * (t1 - t0))
            if 0.12 <= mid <= max(0.12, duration_s - max(0.35, end_card_s)):
                times.append(mid)
    if not times and duration_s > 0.2:
        end_skip = min(max(0.08, end_card_s), max(0.05, duration_s * 0.12))
        step = 0.5 if duration_s <= 12 else 0.75
        t = min(0.25, max(0.04, duration_s * 0.2))
        while t < duration_s - end_skip:
            times.append(t)
            t += step
        mid = max(0.04, duration_s * 0.45)
        if end_skip < mid < duration_s - end_skip * 0.5:
            times.append(mid)
    # de-dupe, keep order
    out, seen = [], set()
    for t in times:
        key = round(t, 2)
        if key not in seen:
            seen.add(key)
            out.append(float(t))
    return out[:24]


def _ffmpeg_frame(path: Path, t: float) -> np.ndarray | None:
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
    )
    r = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(path), "-ss", f"{max(0.0, t):.3f}",
         "-frames:v", "1", "-vf", vf,
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    need = W * H * 3
    buf = r.stdout
    if len(buf) < need:
        return None
    return np.frombuffer(buf[:need], dtype=np.uint8).reshape((H, W, 3))


def _probe_wh(path: Path) -> tuple[int, int]:
    try:
        o = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            text=True, timeout=20,
        ).strip()
        w, h = o.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def _probe_dur(path: Path) -> float:
    try:
        o = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            text=True, timeout=20,
        ).strip()
        return float(o or 0)
    except Exception:
        return 0.0


def pick_best_frame(video: Path, times: list[float] | None = None,
                    edl: list[dict] | None = None) -> tuple[float, np.ndarray] | None:
    dur = _probe_dur(video)
    ts = list(times or []) or candidate_times(dur, edl)
    best_t, best_fr, best_sc = 0.0, None, -1e9
    for t in ts:
        fr = _ffmpeg_frame(video, t)
        if fr is None:
            continue
        sc = score_frame(fr)
        if sc > best_sc:
            best_t, best_fr, best_sc = float(t), fr, sc
    if best_fr is not None and best_sc >= 0:
        return best_t, best_fr
    # Last resort: least-bad extracted frame so a dark first frame is not left as the poster.
    fallback_t, fallback_fr, fallback_sc = 0.0, None, -1e9
    for t in ts:
        fr = _ffmpeg_frame(video, t)
        if fr is None:
            continue
        sc = sharpness(fr) + contrast(fr) + mean_luma(fr)
        if sc > fallback_sc:
            fallback_t, fallback_fr, fallback_sc = float(t), fr, sc
    if fallback_fr is None:
        return None
    return fallback_t, fallback_fr


def write_cover(frame: np.ndarray, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    img = Image.fromarray(frame.astype(np.uint8), mode="RGB")
    if img.size != (W, H):
        # Fit, never crop: contain into 1080x1920 on black (may scale up).
        iw, ih = img.size
        scale = min(W / max(1, iw), H / max(1, ih))
        nw, nh = max(2, int(iw * scale) // 2 * 2), max(2, int(ih * scale) // 2 * 2)
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (W, H), (0, 0, 0))
        canvas.paste(img, ((W - nw) // 2, (H - nh) // 2))
        img = canvas
    img.save(dest, "JPEG", quality=95)
    return dest


def prepend_cover(video: Path, cover: Path, dest: Path | None = None,
                  *, hold_s: float = HOLD_S, max_total: float = MAX_TOTAL_S) -> Path:
    """Put the cover at t=0. Hold it briefly when duration still fits the cap.

    If there is no room, only the first encoded frame is replaced so the
    YouTube/Buffer poster changes without blowing the 32s cap.
    """
    video, cover = Path(video), Path(cover)
    dest = Path(dest or video)
    dur = _probe_dur(video)
    tmp = dest.with_suffix(".cover.tmp.mp4")
    tmp.unlink(missing_ok=True)
    w, h = _probe_wh(video)
    w, h = w or W, h or H
    hold = float(hold_s)
    use_hold = hold >= 1.0 / FPS and (dur + hold) <= max_total + 0.05
    if use_hold:
        fc = (
            f"[0:v]fps={FPS},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p,setsar=1[c];"
            f"[1:v]fps={FPS},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p[v];"
            f"[c][v]concat=n=2:v=1:a=0[outv];"
            f"[1:a]adelay={int(hold * 1000)}|{int(hold * 1000)},asetpts=PTS-STARTPTS[outa]"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-t", f"{hold:.3f}", "-i", str(cover),
            "-i", str(video),
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "17",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(tmp),
        ]
    else:
        # Replace only frame 0 (thumbnail) — duration unchanged.
        fc = (
            f"[1:v]fps={FPS},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p[c];"
            f"[0:v]fps={FPS},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p[v];"
            f"[v][c]overlay=0:0:enable='eq(n,0)'[outv]"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-i", str(cover),
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "17",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "copy",
            "-movflags", "+faststart", str(tmp),
        ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or r.stdout or "cover mux failed")[-300:])
    tmp.replace(dest)
    return dest


def apply_opening_cover(video: Path, *, edl: list[dict] | None = None,
                        hook: str = "", work: Path | None = None,
                        hold_s: float = HOLD_S) -> Path | None:
    """Pick the best frame, write cover.jpg, and make it the opening frame."""
    video = Path(video)
    work = Path(work) if work else video.parent
    work.mkdir(parents=True, exist_ok=True)
    picked = pick_best_frame(video, edl=edl)
    if not picked:
        print("  cover: no usable dramatic frame; leaving first frame as-is")
        return None
    t, frame = picked
    cover_path = work / "cover.jpg"
    write_cover(frame, cover_path)
    meta = {"t": round(float(t), 3), "score": round(float(score_frame(frame)), 3),
            "hook": hook or "", "file": str(cover_path)}
    (work / "cover.json").write_text(json.dumps(meta, indent=2))
    prepend_cover(video, cover_path, video, hold_s=hold_s)
    print(f"  cover: opening poster from t={t:.2f}s -> {cover_path.name}")
    return cover_path
