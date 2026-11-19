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

v10 fix: the final scan used to flag OUR OWN white hook/caption bar ("text caption
at top") on every frame, so every passed render was rejected and the job looped to
the 150-min timeout. The bar rows are now blanked (grey) before Gemini sees the
frame, using work/layout.json (or auto-detected when the layout is missing).
In-scene text (T-shirt prints, signs) with no brand/logo word is not a logo hit.
"""
from __future__ import annotations

import re
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
IGNORE only: flat grey blanked bands (our own caption bar was masked out there), a plain white caption
bar with black text above the picture (that is ours), and text that is physically part of the scene
(shop signs, T-shirt prints, name tags, books).
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


# A hit that only describes in-world text (no brand / logo / watermark word) is not a logo.
_BRAND_RX = re.compile(r"logo|watermark|wordmark|bug|subscribe|netflix|crunchyroll|prime|amazon|disney|hulu|hbo|"
                       r"\bmax\b|apple|paramount|peacock|funimation|hidive|aniplex|toho|mappa|ufotable|muse|ani-one|"
                       r"bilibili|warner|sony|universal|a24|channel|network|studio|copyright|\(c\)|©|"
                       r"coming soon|only on|watch now|release|rating|subtitle|title card|end card|intro card", re.I)
_INSCENE_RX = re.compile(r"shirt|jersey|jacket|uniform|cap\b|hat\b|name ?tag|badge|sign\b|signage|shop|store|"
                         r"street|book|newspaper|letter|note\b|poster on|graffiti|label on|menu", re.I)
_OURBAR_RX = re.compile(r"(gr[ae]y|blank|masked|empty)\s*(band|bar|box|area|strip)", re.I)


def real_hit(what: str) -> bool:
    """False for in-scene text with no brand/logo word and for our masked bar."""
    w = str(what or "")
    if _OURBAR_RX.search(w) and not _BRAND_RX.search(w):
        return False
    if _INSCENE_RX.search(w) and not _BRAND_RX.search(w):
        return False
    return True


def _grab(video: Path, t: float, dst: Path, w: int, mask=None) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", f"scale={w}:-2", str(dst)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if mask and dst.exists():
        apply_mask(dst, mask)
    return dst


def apply_mask(img_path: Path, mask) -> None:
    """Paint rows (y0_frac, y1_frac) flat grey: our own caption bar is never a logo."""
    from PIL import Image, ImageDraw
    im = Image.open(img_path).convert("RGB")
    W, H = im.size
    dr = ImageDraw.Draw(im)
    for y0, y1 in mask:
        dr.rectangle([0, max(0, int(y0 * H) - 2), W, min(H, int(round(y1 * H)) + 2)], fill=(128, 128, 128))
    im.save(img_path, quality=92)


def detect_caption_band(img_path: Path):
    """Fallback when layout.json is missing: the widest band of mostly-white full-width rows
    (our hook bar: white with black text). Returns (y0_frac, y1_frac) or None."""
    try:
        from PIL import Image
        im = Image.open(img_path).convert("L")
    except Exception:
        return None
    W, H = im.size
    px = im.load()
    white = []
    for y in range(H):
        n = sum(1 for x in range(0, W, 4) if px[x, y] >= 225)
        white.append(n / max(1, len(range(0, W, 4))) >= 0.6)
    best, cur, start = (0, 0), 0, 0
    for y, wv in enumerate(white + [False]):
        if wv:
            if cur == 0:
                start = y
            cur += 1
        else:
            if cur > best[1] - best[0]:
                best = (start, start + cur)
            cur = 0
    if best[1] - best[0] < 0.03 * H or best[1] - best[0] > 0.25 * H:
        return None
    return best[0] / H, best[1] / H


def layout_mask(layout: dict | None, frame_h: int = 1920):
    """Caption bar rows from pipeline layout.json (1080x1920 master coords)."""
    try:
        y, h = float(layout["bar_y"]), float(layout["bar_h"])
    except Exception:
        return None
    if h <= 0:
        return None
    return [(y / frame_h, (y + h) / frame_h)]


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
         fps: float = 2.0, w: int = 480, vertical: bool = False, ask=None, mask=None) -> dict:
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
            frames.append(_grab(Path(video), t, work / f"f{len(frames):04d}.jpg", w, mask=mask))
            owner.append((key, t))
    if not frames:
        return {"ok": True, "hits": {}, "frames": 0}
    per, cols = (9, 3) if vertical else (24, 4)
    sheets = _sheets(frames, work, tag, per, cols, 540 if vertical else w)
    if ask is None:
        import gemini
        ask = lambda p, imgs: gemini.generate_json(p, imgs, purpose=f"logo_{tag}", temperature=0.0, timeout=150.0)
    hits: dict[str, list[str]] = {}
    ignored: list[str] = []
    per_call = 6 * per
    for g in range(0, len(sheets), 6):  # <= 6 sheets per call
        out = None
        for _try in range(2):  # one retry on a transient miss; never an endless loop
            out = ask(PROMPT.format(brands=BRANDS), sheets[g:g + 6])
            if isinstance(out, dict) and isinstance(out.get("flagged"), list):
                break
        if not isinstance(out, dict) or not isinstance(out.get("flagged"), list):
            return {"ok": None, "hits": hits, "frames": len(frames), "error": "no gemini verdict"}
        for r in out["flagged"]:
            lo, hi = g * per, min(len(owner), g * per + per_call)
            try:
                i = int(r.get("i"))
            except Exception:
                i = -1
            if not (lo <= i < hi):
                # labels are global; a per-call number is shifted, anything else is pinned to
                # this call's first frame (fail-safe: a flagged logo is never dropped)
                i = i + lo if 0 <= i + lo < hi else lo
            key, t = owner[i]
            what = str(r.get("what") or "logo")[:60]
            if not real_hit(what):
                ignored.append(f"{t:.2f}s {what}")
                continue
            hits.setdefault(key, []).append(f"{t:.2f}s {what}")
    res = {"ok": not hits, "hits": hits, "frames": len(frames)}
    if ignored:
        res["ignored"] = ignored[:20]
    if mask:
        res["masked_rows"] = [[round(a, 4), round(b, 4)] for a, b in mask]
    return res


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


def scan_video(video: Path, work: Path, ask=None, layout: dict | None = None) -> dict:
    """Final gate: whole output at 2 fps (every frame of every cut, start to end).

    Our own caption bar rows are blanked first (layout.json, else auto-detected)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except Exception:
        return {"ok": None, "hits": {}, "frames": 0, "error": "probe failed"}
    mask = layout_mask(layout)
    if mask is None:
        probe_img = Path(work) / "logo_final_probe.jpg"
        Path(work).mkdir(parents=True, exist_ok=True)
        _grab(Path(video), min(1.0, dur / 2), probe_img, 540)
        band = detect_caption_band(probe_img)
        mask = [band] if band else None
    return scan(video, [("final", 0.0, dur)], work, tag="final", fps=2.0, vertical=True, ask=ask, mask=mask)
