"""Low-memory chunked render helpers for heavy official-clip Shorts.

Split a short edit timeline into 4-5 segments, encode each at 1080x1920,
concat with the demuxer (same codec), mux music once over the full
timeline, then upscale to 2160x3840 lanczos. Used automatically for long
or high-bitrate sources and for items whose last render was cancelled.
Light items keep the single-pass path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

HEAVY_SOURCE_S = 90.0
HEAVY_BITRATE = 8_000_000
STAB_WINDOW_S = 40.0
BUNDLE_CAP_S = 210.0
SEGMENTS = 5
SEG_RETRIES = 2
DEFAULT_THREADS = 2

PREV_CANCEL_MARKERS = (
    "shutdown",
    "cancelled",
    "canceled",
    "no result json",
    "timed out or cancelled",
    "runner has received a shutdown",
)

_ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.:](\d{2})")


def ffmpeg_threads() -> int:
    try:
        return max(1, min(4, int(os.environ.get("SKY_FFMPEG_THREADS") or DEFAULT_THREADS)))
    except Exception:
        return DEFAULT_THREADS


def ffmpeg_thread_args() -> list[str]:
    n = str(ffmpeg_threads())
    return ["-threads", n, "-filter_threads", n]


def chunked_mode() -> str:
    return (os.environ.get("SKY_CHUNKED") or "auto").strip().lower()


def chunked_active() -> bool:
    mode = chunked_mode()
    if mode in ("0", "off", "false", "no"):
        return False
    if mode in ("1", "on", "true", "yes", "always"):
        return True
    return os.environ.get("SKY_CHUNKED_ACTIVE") == "1"


def is_heavy(*, duration_s: float = 0, width: int = 0, height: int = 0,
             bitrate: float = 0, last_error: str = "", item: dict | None = None) -> bool:
    if item and (item.get("chunked") or item.get("chunk_render")):
        return True
    err = (last_error or "").lower()
    if any(m in err for m in PREV_CANCEL_MARKERS):
        return True
    dur = float(duration_s or 0)
    if dur >= HEAVY_SOURCE_S:
        return True
    if float(bitrate or 0) >= HEAVY_BITRATE and dur >= 45:
        return True
    if min(int(width or 0), int(height or 0)) >= 2160 and dur >= 45:
        return True
    return False


def last_error_blob(repo: Path | None, item: dict | None) -> str:
    """Join last_error/detail from this id and earlier same-topic results."""
    if not repo or not item:
        return str((item or {}).get("last_error") or "")
    rid = str(item.get("id") or "")
    words = " ".join(str(item.get("words") or "").lower().split())
    slug = rid.split("-")[-1] if rid else ""
    parts = [str(item.get("last_error") or "")]
    res = Path(repo) / "results"
    if not res.is_dir():
        return " ".join(parts)
    for p in res.glob("*.json"):
        try:
            doc = json.loads(p.read_text())
        except Exception:
            continue
        did = str(doc.get("id") or p.stem)
        same = did == rid
        if not same and slug and slug in did.lower():
            same = True
        if not same and words:
            dw = " ".join(str(doc.get("words") or "").lower().split())
            same = bool(dw) and dw == words
        if not same:
            continue
        parts.append(str(doc.get("last_error") or ""))
        parts.append(str(doc.get("detail") or ""))
    return " ".join(parts)


def probe_media(path: Path | str | None) -> dict:
    out = {"duration": 0.0, "width": 0, "height": 0, "bitrate": 0.0}
    if not path or not Path(path).exists():
        return out
    try:
        raw = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,bit_rate:format=duration,bit_rate",
                "-of", "json", str(path),
            ],
            text=True, timeout=30,
        )
        data = json.loads(raw or "{}")
        fmt = data.get("format") or {}
        st = (data.get("streams") or [{}])[0]
        out["duration"] = float(fmt.get("duration") or 0)
        out["width"] = int(st.get("width") or 0)
        out["height"] = int(st.get("height") or 0)
        br = st.get("bit_rate") or fmt.get("bit_rate") or 0
        out["bitrate"] = float(br or 0)
    except Exception:
        pass
    return out


def activate_for_source(src, item: dict | None = None, repo: Path | None = None) -> bool:
    """Set SKY_CHUNKED_ACTIVE from SKY_CHUNKED (auto/on/off) and source weight."""
    mode = chunked_mode()
    if mode in ("0", "off", "false", "no"):
        os.environ["SKY_CHUNKED_ACTIVE"] = "0"
        return False
    if mode in ("1", "on", "true", "yes", "always"):
        os.environ["SKY_CHUNKED_ACTIVE"] = "1"
        return True
    meta = probe_media(src)
    err = last_error_blob(repo, item)
    heavy = is_heavy(
        duration_s=meta.get("duration") or 0,
        width=meta.get("width") or 0,
        height=meta.get("height") or 0,
        bitrate=meta.get("bitrate") or 0,
        last_error=err,
        item=item,
    )
    os.environ["SKY_CHUNKED_ACTIVE"] = "1" if heavy else "0"
    if heavy:
        print(
            f"  chunked path: source {meta.get('duration'):.0f}s "
            f"{meta.get('width')}x{meta.get('height')} (auto)",
            flush=True,
        )
    return heavy


def segment_count(duration_s: float, n: int = SEGMENTS) -> int:
    dur = max(0.0, float(duration_s or 0))
    want = max(4, min(5, int(n or SEGMENTS)))
    if dur <= 8:
        return 2
    if dur <= 16:
        return min(4, want)
    return want


def segment_bounds(duration_s: float, n: int | None = None) -> list[tuple[float, float]]:
    dur = max(0.0, float(duration_s or 0))
    k = n or segment_count(dur)
    k = max(1, int(k))
    if dur <= 0:
        return [(0.0, 0.0)]
    step = dur / k
    bounds = []
    for i in range(k):
        t0 = i * step
        t1 = dur if i == k - 1 else (i + 1) * step
        if t1 - t0 > 0.01:
            bounds.append((t0, t1))
    return bounds or [(0.0, dur)]


def split_timeline(shots: list[dict], n: int = SEGMENTS, total: float | None = None) -> list[list[dict]]:
    """Pack EDL shots into n segments. Do not split a shot unless it is longer than a bin."""
    items = [s for s in (shots or []) if s is not None]
    if not items:
        return [[] for _ in range(max(1, segment_count(total or 0, n)))]
    if total is None:
        total = max(float(s.get("t1") or 0) for s in items)
    k = segment_count(total, n)
    bins: list[list[dict]] = [[] for _ in range(k)]
    if total <= 0:
        bins[0] = list(items)
        return bins
    step = total / k

    def _t0(s):
        return float(s.get("t0") or s.get("start") or 0)

    def _t1(s):
        if s.get("t1") is not None:
            return float(s["t1"])
        return _t0(s) + float(s.get("dur") or s.get("duration") or 0)

    for s in items:
        mid = (_t0(s) + _t1(s)) / 2.0
        idx = min(k - 1, max(0, int(mid / step)))
        bins[idx].append(s)
    # Drop empty trailing bins but keep at least one.
    while len(bins) > 1 and not bins[-1]:
        bins.pop()
    return bins


def stab_windows(duration_s: float, window_s: float = STAB_WINDOW_S) -> list[tuple[float, float]]:
    dur = max(0.0, float(duration_s or 0))
    win = max(20.0, float(window_s or STAB_WINDOW_S))
    if dur <= win + 2:
        return [(0.0, dur)] if dur else [(0.0, 0.0)]
    n = max(4, min(5, int((dur + win - 0.01) // win) or 4))
    n = min(8, max(n, int((dur + 39.0) // 40) or 4))
    # Prefer 4-5 windows; allow more only for very long leftover sources.
    if dur <= 220:
        n = max(4, min(5, int((dur + win - 0.01) // win) or 4))
    return segment_bounds(dur, n)


def should_chunk_stabilize(duration_s: float) -> bool:
    return chunked_active() or float(duration_s or 0) >= HEAVY_SOURCE_S


def write_concat_list(paths, list_path) -> Path:
    dest = Path(list_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ffconcat version 1.0"]
    for p in paths:
        s = Path(p).resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{s}'")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def concat_demuxer(paths, dest, *, reencode: bool = False) -> Path:
    dest = Path(dest)
    if not paths:
        raise RuntimeError("concat_demuxer: no segment paths")
    if len(paths) == 1:
        shutil.copy2(paths[0], dest)
        return dest
    lst = dest.with_suffix(dest.suffix + ".concat.txt")
    write_concat_list(paths, lst)
    cmd = [
        "ffmpeg", "-y", *ffmpeg_thread_args(),
        "-f", "concat", "-safe", "0", "-i", str(lst),
    ]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "17",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 1000:
        if not reencode:
            return concat_demuxer(paths, dest, reencode=True)
        raise RuntimeError(f"concat failed: {(r.stderr or r.stdout or '')[-400:]}")
    return dest


def mux_music(video, audio, dest, duration: float | None = None) -> Path:
    """Copy the concatenated video and mux the already-prepared full-timeline bed."""
    dest = Path(dest)
    cmd = [
        "ffmpeg", "-y", *ffmpeg_thread_args(),
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest",
        str(dest),
    ]
    if duration:
        # -t after inputs limits the mux without re-encoding video.
        cmd = cmd[:-1] + ["-t", f"{float(duration):.3f}", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 1000:
        raise RuntimeError(f"mux_music failed: {(r.stderr or r.stdout or '')[-400:]}")
    return dest


def upscale_args() -> list[str]:
    return [
        "-vf", "scale=2160:3840:flags=lanczos",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
    ]


def upscale_4k_chunked(src, dest, n: int | None = None) -> Path:
    """Upscale 1080x1920 (with already-muxed audio) in time slices, then remux original audio."""
    src = Path(src)
    dest = Path(dest)
    meta = probe_media(src)
    dur = float(meta.get("duration") or 0)
    bounds = segment_bounds(dur, n or segment_count(dur))
    work = dest.parent / f"{dest.stem}_up4k"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    audio = work / "audio.m4a"
    try:
        ar = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "copy", str(audio)],
            capture_output=True, text=True,
        )
        if ar.returncode != 0 or not audio.exists():
            subprocess.check_call(
                ["ffmpeg", "-y", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "192k", str(audio)],
            )
        parts = []
        for i, (t0, t1) in enumerate(bounds):
            part = work / f"u{i:02d}.mp4"

            def _one(t0=t0, t1=t1, part=part):
                r = subprocess.run(
                    [
                        "ffmpeg", "-y", *ffmpeg_thread_args(),
                        "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(src),
                        *upscale_args(), "-an", "-movflags", "+faststart", str(part),
                    ],
                    capture_output=True, text=True,
                )
                if r.returncode != 0 or not part.exists() or part.stat().st_size < 1000:
                    raise RuntimeError((r.stderr or r.stdout or "upscale segment failed")[-400:])

            run_with_retry(_one, retries=SEG_RETRIES)
            parts.append(part)
        silent = work / "video4k.mp4"
        concat_demuxer(parts, silent)
        mux_music(silent, audio, dest, duration=dur or None)
        return dest
    finally:
        free_tree(work)


def run_with_retry(fn, retries: int = SEG_RETRIES):
    """Run fn; retry up to `retries` times (2x means 3 attempts)."""
    last = None
    tries = max(1, int(retries) + 1)
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i + 1 >= tries:
                raise
    raise last or RuntimeError("retry failed")


def _ass_to_s(h, m, s, cs) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


def _s_to_ass(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        cs = 0
        s += 1
        if s >= 60:
            s = 0
            m += 1
            if m >= 60:
                m = 0
                h += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def shift_ass_file(src, dst, offset_s: float) -> Path:
    """Add offset_s to every Dialogue/Comment start and end time."""
    text = Path(src).read_text(encoding="utf-8", errors="replace")

    def _shift_tok(tok: str) -> str:
        m = _ASS_TIME.match(tok.strip())
        if not m:
            return tok
        return _s_to_ass(_ass_to_s(*m.groups()) + float(offset_s))

    lines = []
    for line in text.splitlines():
        if line.startswith("Dialogue:") or line.startswith("Comment:"):
            parts = line.split(",", 9)
            if len(parts) >= 3:
                parts[1] = _shift_tok(parts[1])
                parts[2] = _shift_tok(parts[2])
                line = ",".join(parts)
        lines.append(line)
    dest = Path(dst)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def swap_ass_path(vfilt: str, new_ass: Path) -> str:
    esc = Path(new_ass).as_posix().replace(":", r"\:")
    return re.sub(r"ass=[^,:]+", f"ass={esc}", vfilt or "")


def render_overlay_segments(
    *, silent, audio, bar, vfilt: str, dur: float, dest, fps: int = 30,
    work, ass_path=None, retries: int = SEG_RETRIES,
) -> Path:
    """Render the whitebar overlay in 4-5 1080x1920 segments (video only), then concat."""
    dest = Path(dest)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    bounds = segment_bounds(dur)
    parts = []
    for i, (t0, t1) in enumerate(bounds):
        part = work / f"seg_{i:02d}.mp4"
        vf = vfilt
        if ass_path and Path(ass_path).exists():
            shifted = work / f"seg_{i:02d}.ass"
            shift_ass_file(ass_path, shifted, -t0)
            vf = swap_ass_path(vfilt, shifted)

        def _one(t0=t0, t1=t1, part=part, vf=vf):
            cmd = [
                "ffmpeg", "-y", *ffmpeg_thread_args(),
                "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(silent),
                "-t", f"{t1 - t0:.3f}", "-i", str(audio),
                "-i", str(bar),
                "-filter_complex", vf,
                "-map", "[v]", "-an",
                "-c:v", "libx264", "-preset", "fast", "-crf", "17",
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-movflags", "+faststart", str(part),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not part.exists() or part.stat().st_size < 1000:
                raise RuntimeError((r.stderr or r.stdout or "segment render failed")[-500:])

        run_with_retry(_one, retries=retries)
        parts.append(part)
        print(f"  chunked segment {i + 1}/{len(bounds)} {t0:.2f}-{t1:.2f}s ok", flush=True)
    concat_demuxer(parts, dest)
    return dest


def free_paths(*paths) -> None:
    for p in paths:
        if not p:
            continue
        q = Path(p)
        try:
            if q.is_dir():
                shutil.rmtree(q, ignore_errors=True)
            elif q.exists():
                q.unlink()
        except Exception:
            pass


def free_tree(path) -> None:
    free_paths(path)
