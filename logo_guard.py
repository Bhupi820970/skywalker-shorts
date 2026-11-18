"""Hard no-logo guard: streamer/studio cards, corner bugs, channel watermarks, show-title text.

Why: v9 Dandadan shipped a Netflix "N SUBSCRIBE" end card (source tail) and a
"DAN DA DAN SEASON 2" corner bug. The pixel heuristics ignore corner bugs by
design, rank_scenes saw one frame per shot for only the top 40 shots, and the QC
judge noticed "Netflix Subscribe watermark" but only scored clean_footage 8.

Three layers, all hard (a hit is never shipped):
1. edge_guard: never cut from the first/last seconds of a source (studio/streamer
   intro and end-screen cards live there).
2. scan_scenes: dense (2 fps) Gemini scan of every candidate scene BEFORE the cut;
   flagged scenes are skipped so the edit falls through to clean scenes, and if
   too little remains the pipeline fails and render_item falls through to the next
   clip/source/title/backup.
3. scan_video: dense (2 fps) Gemini scan of the whole FINAL video in the publish
   gate. Any hit (or no verdict) blocks publishing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

BRANDS = ("Netflix (red N, NETFLIX wordmark, 'SUBSCRIBE' end card), Crunchyroll, Prime Video / Amazon, "
          "Disney+, Hulu, HBO / Max, Apple TV+, Paramount+, Peacock, Funimation, HIDIVE, Aniplex, Toho, "
          "TOHO animation, MAPPA, Ufotable, Muse, Ani-One, Bilibili, Warner Bros, Sony, Universal, A24, "
          "any TV network or YouTube channel watermark")

PROMPT = """You are a strict broadcast compliance checker. Each tile is one frame, labelled with a yellow index number.
Flag EVERY frame that shows ANY of these, even if tiny, faint, semi-transparent, in a corner or at the edge:
- a streaming/studio/distributor/network logo or wordmark: {brands}
- a corner bug / channel watermark / "SUBSCRIBE" / "Watch now" / "Only on" / "Coming soon" / release date / rating card
- a show or film TITLE text or logotype on the picture (e.g. "DAN DA DAN SEASON 2"), studio intro or end-screen card
- burned-in subtitles or any other text printed on the footage
IGNORE only: a plain white caption bar with black text at the very top of a vertical frame (that is ours),
and text that is physically part of the scene (shop signs, T-shirt prints, books).
When unsure, FLAG it. Return JSON: {{"flagged": [{{"i": int, "what": "short description"}}]}}"""

HEAD_MAX, HEAD_FRAC = 3.0, 0.06
TAIL_MAX, TAIL_FRAC = 15.0, 0.18


def edge_guard(dur: float) -> tuple[float, float]:
    """(head, tail) seconds never to cut from: intro logo cards and end-screen cards."""
    dur = max(0.0, float(dur or 0))
    return min(HEAD_MAX, HEAD_FRAC * dur), min(TAIL_MAX, TAIL_FRAC * dur)


def apply_edge_guard(scenes: list[dict], dur: float, min_len: float = 0.6) -> int:
    """Trim/skip scenes that touch the guarded head/tail of the source. Returns #changed."""
    head, tail = edge_guard(dur)
    lo, hi = head, float(dur) - tail
    n = 0
    for s in scenes:
        if s.get("skip"):
            continue
        a, b = float(s["start"]), float(s["end"])
        if a >= lo and b <= hi:
            continue
        na, nb = max(a, lo), min(b, hi)
        if nb - na < min_len:
            s["skip"] = True
            s["reason"] = s["card_reason"] = "edge_card_guard"
            s["score"] = 0
        else:
            s["start"], s["end"], s["dur"] = round(na, 3), round(nb, 3), round(nb - na, 3)
        n += 1
    return n


def sample_times(t0: float, t1: float, fps: float = 2.0) -> list[float]:
    """Dense sample times inside [t0, t1]: every 1/fps s plus both ends, at least 2."""
    t0, t1 = float(t0), float(t1)
    if t1 - t0 < 0.08:
        return [t0]
    a, b = t0 + 0.04, t1 - 0.04
    ts = [a]
    step = 1.0 / fps
    t = a + step
    while t < b - 0.05:
        ts.append(round(t, 3))
        t += step
    ts.append(b)
    return ts


def _grab(video: Path, t: float, dst: Path, w: int) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", f"scale={w}:-2", str(dst)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def _sheets(frames: list[Path], work: Path, tag: str, per: int, cols: int, w: int) -> list[Path]:
    from PIL import Image, ImageDraw
    out = []
    for k in range(0, len(frames), per):
        chunk = frames[k:k + per]
        ims = []
        for f in chunk:
            try:
                ims.append(Image.open(f).convert("RGB"))
            except Exception:
                ims.append(Image.new("RGB", (w, w * 9 // 16)))
        th = max(im.size[1] * w // max(1, im.size[0]) for im in ims)
        rows = (len(ims) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * w, rows * th), (0, 0, 0))
        dr = ImageDraw.Draw(sheet)
        for j, im in enumerate(ims):
            im = im.resize((w, im.size[1] * w // max(1, im.size[0])))
            x, y = (j % cols) * w, (j // cols) * th
            sheet.paste(im, (x, y))
            dr.rectangle([x, y + th // 2 - 14, x + 40, y + th // 2 + 14], fill=(255, 255, 0))
            dr.text((x + 4, y + th // 2 - 6), str(k + j), fill=(0, 0, 0))
        p = work / f"logo_{tag}_{k // per:02d}.jpg"
        sheet.save(p, quality=88)
        out.append(p)
    return out


def scan(video: Path, windows: list[tuple[str, float, float]], work: Path, *, tag: str,
         fps: float = 2.0, w: int = 480, vertical: bool = False, ask=None) -> dict:
    """Gemini scan of every window at `fps`. Returns {"ok": bool|None, "hits": {key: [what]}, "frames": n}.

    ok=None means no verdict (Gemini unavailable) -> callers treat it as NOT clean.
    """
    work = Path(work) / f"logo_{tag}"
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob("*"):
        f.unlink()
    frames, owner = [], []
    for key, t0, t1 in windows:
        for t in sample_times(t0, t1, fps):
            frames.append(_grab(Path(video), t, work / f"f{len(frames):04d}.jpg", w))
            owner.append((key, t))
    if not frames:
        return {"ok": True, "hits": {}, "frames": 0}
    per, cols = (9, 3) if vertical else (24, 4)
    sheets = _sheets(frames, work, tag, per, cols, 540 if vertical else w)
    if ask is None:
        import gemini
        ask = lambda p, imgs: gemini.generate_json(p, imgs, purpose=f"logo_{tag}", temperature=0.0, timeout=150.0)
    hits: dict[str, list[str]] = {}
    for g in range(0, len(sheets), 6):  # <= 6 sheets per call
        out = ask(PROMPT.format(brands=BRANDS), sheets[g:g + 6])
        if not isinstance(out, dict) or not isinstance(out.get("flagged"), list):
            return {"ok": None, "hits": hits, "frames": len(frames), "error": "no gemini verdict"}
        for r in out["flagged"]:
            try:
                i = int(r.get("i"))
                key, t = owner[i]
            except Exception:
                continue
            hits.setdefault(key, []).append(f"{t:.2f}s {str(r.get('what') or 'logo')[:60]}")
    return {"ok": not hits, "hits": hits, "frames": len(frames)}


def scan_scenes(mezz: Path, scenes: list[dict], work: Path, ask=None) -> dict:
    """Pre-cut: scan every usable scene; skip flagged ones (hard, no min-footage exception)."""
    kept = [s for s in scenes if not s.get("skip")]
    win = [(f"{float(s['start']):.2f}-{float(s['end']):.2f}", float(s["start"]), float(s["end"])) for s in kept]
    res = scan(mezz, win, work, tag="scenes", fps=2.0, ask=ask)
    for s in kept:
        k = f"{float(s['start']):.2f}-{float(s['end']):.2f}"
        if k in res["hits"]:
            s["skip"] = True
            s["reason"] = s["card_reason"] = "gemini_logo"
            s["logo"] = res["hits"][k][:3]
            s["score"] = 0
    return res


def scan_video(video: Path, work: Path, ask=None) -> dict:
    """Final gate: whole output at 2 fps (every frame of every cut, start to end)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except Exception:
        return {"ok": None, "hits": {}, "frames": 0, "error": "probe failed"}
    return scan(video, [("final", 0.0, dur)], work, tag="final", fps=2.0, vertical=True, ask=ask)
