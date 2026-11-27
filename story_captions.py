"""Neon story captions (pro-edit caption system, ported) — an optional style for SOME Skywalker videos.

Skywalker edits are song-only (no character dialogue), so these are NOT speech subtitles: they are short
on-screen story beats built from the English hook / title / description, timed to the song's beats.
Never song lyrics (the song text is never an input). Look = pro-edit's approved captions: Anton font,
hard black drop shadow + outline, 80% -> 100% pop-in, big neon key words + small white fillers, every key
word a different colour from the pattern palette, placed BESIDE a face on the side away from it (YuNet),
never over a face, alternating left/right when no face is visible, inside the picture's safe area.
The white hook bar above the picture is unchanged.

Rotation: about 1 in 3 videos (deterministic per item id, so a re-render keeps its style). A queue item
can force it with "caption_style": "neon" | "none"; env SKY_STORY_CAPTIONS=1|0 overrides everything.
The 15 patterns (caption_patterns.py) rotate; a pattern is not reused within 2.5 days (results history).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import caption_patterns as CP

ROOT = Path(__file__).resolve().parent
FONT = ROOT / "fonts" / "PlayfairDisplay.ttf"
YUNET = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
W, H = 1080, 1920
EVERY = 3
PATTERN_COOLDOWN_DAYS = 2.5
FILLERS = {
    "a", "an", "the", "to", "of", "and", "or", "but", "in", "on", "at", "for", "with", "by", "from", "is", "are",
    "was", "were", "be", "been", "it", "its", "he", "she", "his", "her", "him", "they", "them", "their", "you",
    "your", "i", "me", "my", "we", "our", "this", "that", "these", "those", "who", "what", "why", "how", "when",
    "just", "so", "as", "into", "than", "then", "only", "even", "all", "has", "had", "have", "do", "did", "does",
    "not", "no", "up", "out", "about", "after", "before", "over", "s", "t",
}
_SKIP_LINE = re.compile(r"(^#|edit by|source:|all rights|official .* clips|music:|song:|license|http|#edit|#shorts)", re.I)


def _h(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest(), 16)


def wanted(item: dict | None, item_id: str = "", env: str | None = None) -> bool:
    """~1 in 3 videos get neon story captions (deterministic per id)."""
    if env in ("0", "1"):
        return env == "1"
    style = str((item or {}).get("caption_style") or "").lower()
    if style in ("neon", "story", "pro"):
        return True
    if style in ("none", "off", "clean"):
        return False
    iid = item_id or (item or {}).get("id") or ""
    return bool(iid) and _h("neon|" + iid) % EVERY == 0


def recent_patterns(results_dir: Path | None, now: datetime | None = None) -> set[str]:
    ist = timezone(timedelta(hours=5, minutes=30))
    now = now or datetime.now(ist)
    out: set[str] = set()
    if not results_dir or not Path(results_dir).exists():
        return out
    for p in Path(results_dir).glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        pid = r.get("caption_pattern")
        if not pid:
            continue
        at = str(r.get("slot") or r.get("at") or "")
        try:
            dt = datetime.fromisoformat(at.replace("Z", "+00:00").replace(" IST", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ist)
        except Exception:
            continue
        if now - dt <= timedelta(days=PATTERN_COOLDOWN_DAYS):
            out.add(str(pid))
    return out


def pick_pattern(item_id: str, results_dir: Path | None = None, forced: str | None = None) -> dict:
    if forced and forced in CP.BY_ID:
        return CP.get(forced)
    avoid = recent_patterns(results_dir)
    ids = [p for p in CP.ROTATION if p not in avoid] or list(CP.ROTATION)
    return CP.get(ids[_h("pattern|" + (item_id or "x")) % len(ids)])


def story_lines(hook: str = "", title: str = "", description: str = "", words: str = "") -> list[str]:
    """Story beats from OUR text (hook first, then the description), never lyrics."""
    out: list[str] = []
    seen = set()
    cands = [hook]
    for line in (description or "").splitlines():
        line = line.strip()
        if not line or _SKIP_LINE.search(line):
            continue
        cands += re.split(r"(?<=[.!?])\s+", line)
    if title:
        cands.append(re.split(r"\s*[|#]", title)[0])
    for c in cands:
        c = re.sub(r"[^\w\s'’?!,.-]", " ", c or "")
        c = " ".join(c.split()).strip(" .,-")
        k = c.lower()
        if len(c) < 3 or k in seen or not re.search(r"[A-Za-z]", c):
            continue
        seen.add(k)
        out.append(c)
    return out[:4]


def chunks(lines: list[str], per: int, max_chunks: int = 8) -> list[list[str]]:
    out = []
    for ln in lines:
        ws = ln.split()
        i = 0
        while i < len(ws):
            take = per
            # keep a dangling single filler with the next words
            grp = ws[i:i + take]
            out.append(grp)
            i += take
            if len(out) >= max_chunks:
                return out
    return out


def is_key(word: str) -> bool:
    w = re.sub(r"[^a-z']", "", word.lower())
    return bool(w) and w not in FILLERS and len(w) > 1


def beat_slots(beats: list[float], dur: float, n: int, start: float = 1.3, tail: float = 1.6) -> list[tuple[float, float]]:
    """Show each chunk for ~2 beats, one beat apart, after the hook read and before the ending."""
    bs = [float(b) for b in beats or [] if start <= float(b) <= dur - tail]
    if len(bs) < 3:
        step = 0.5
        bs = [start + i * step for i in range(int(max(0, dur - tail - start) / step))]
    out = []
    i = 0
    while len(out) < n and i + 2 < len(bs):
        t0, t1 = bs[i], bs[i + 2]
        if t1 - t0 < 0.65:
            t1 = t0 + 0.65
        t1 = min(t1, t0 + 1.6)
        out.append((round(t0, 3), round(t1, 3)))
        nxt = t1 + 0.25
        while i < len(bs) and bs[i] < nxt:
            i += 1
    return out


def _font(size: int):
    """PIL font that matches what libass draws for ASS \\fs<size> (libass sizes by ascent+descent, not em)."""
    from PIL import ImageFont
    try:
        ref = ImageFont.truetype(str(FONT), 100)
        asc, desc = ref.getmetrics()
        return ImageFont.truetype(str(FONT), max(8, int(round(size * 100.0 / max(1, asc + desc)))))
    except Exception:
        return ImageFont.load_default()


def measure(words: list[str], key_px: int, filler_px: int) -> tuple[int, int]:
    wsum, hmax = 0, 0
    for w in words:
        f = _font(key_px if is_key(w) else filler_px)
        b = f.getbbox(w.upper() + " ")
        wsum += b[2] - b[0]
        hmax = max(hmax, b[3] - b[1])
    return int(wsum), int(hmax * 1.25)


def detect_faces(video: Path, t: float, lay: dict) -> list[tuple[int, int, int, int]]:
    """Faces at time t mapped into 1080x1920 output coords (inside the picture plate)."""
    try:
        import cv2
        import subprocess
        import numpy as np
        if not YUNET.exists():
            return []
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=60)
        if r.returncode != 0 or not r.stdout:
            return []
        fr = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
        if fr is None:
            return []
        h, w = fr.shape[:2]
        det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.6, 0.3, 5000)
        _, faces = det.detect(fr)
        out = []
        sx, sy = float(lay["plate_w"]) / w, float(lay["plate_h"]) / h
        for f in (faces if faces is not None else []):
            x, y, fw, fh = [float(v) for v in f[:4]]
            out.append((int(lay["plate_x"] + x * sx), int(lay["plate_y"] + y * sy), int(fw * sx), int(fh * sy)))
        return out
    except Exception:
        return []


def _overlap(a, b) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + ah, by + bh) - max(ay, by)
    return max(0, dx) * max(0, dy)


def place(words, pat, lay, faces, idx) -> dict | None:
    """Beside the biggest face on the side away from it; alternate sides without a face; never over a face."""
    px, py, pw, ph = int(lay["plate_x"]), int(lay["plate_y"]), int(lay["plate_w"]), int(lay["plate_h"])
    key_px = int(pat["key"] * ph * 2.6) if len(words) <= 2 else int(pat["key_long"] * ph * 2.6)
    fil_px = int(pat["filler"] * ph * 2.6)
    tw, th = measure(words, key_px, fil_px)
    margin = int(0.025 * pw)
    if tw > pw - 2 * margin:
        s = (pw - 2 * margin) / float(tw)
        key_px, fil_px = int(key_px * s), int(fil_px * s)
        tw, th = measure(words, key_px, fil_px)
    ys = pat["y"] or [0.0]
    yoff = ys[idx % len(ys)]
    cands = []
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        fcx, fcy = fx + fw / 2, fy + fh / 2
        side = 1 if fcx < px + pw / 2 else -1
        for s in (side, -side):
            cx = fcx + s * (pat["gap"] * fw + tw / 2)
            cx += (1 if cx < px + pw / 2 else -1) * pat["nudge"] * pw  # nudge toward the centre
            cy = fcy + yoff * fh
            cands.append((cx, cy))
    side = 1 if idx % 2 else -1
    nf_y = py + pat["no_face_y"] * ph + yoff * 0.12 * ph
    cands += [(px + pw / 2 + side * pw * 0.22, nf_y), (px + pw / 2 - side * pw * 0.22, nf_y), (px + pw / 2, nf_y)]
    for _ in range(7):
        for cx, cy in cands:
            x0 = int(min(max(cx - tw / 2, px + margin), px + pw - margin - tw))
            y0 = int(min(max(cy - th / 2, py + margin), py + ph - margin - th))
            box = (x0, y0, tw, th)
            if all(_overlap(box, f) <= 0.02 * tw * th for f in faces):
                return {"box": box, "key_px": key_px, "fil_px": fil_px}
        key_px, fil_px = max(22, int(key_px * 0.85)), max(18, int(fil_px * 0.85))   # shrink until a side slot is clear
        tw, th = measure(words, key_px, fil_px)
        cands = []
        if faces:
            fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            fcx, fcy = fx + fw / 2, fy + fh / 2
            side = 1 if fcx < px + pw / 2 else -1
            for s in (side, -side):
                cx = fcx + s * (pat["gap"] * fw + tw / 2)
                cx += (1 if cx < px + pw / 2 else -1) * pat["nudge"] * pw
                cands.append((cx, fcy + yoff * fh))
        side = 1 if idx % 2 else -1
        nf_y = py + pat["no_face_y"] * ph + yoff * 0.12 * ph
        cands += [(px + pw / 2 + side * pw * 0.22, nf_y), (px + pw / 2 - side * pw * 0.22, nf_y), (px + pw / 2, nf_y)]
    return None


def _ass_color(rgb) -> str:
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600):d}:{int((t % 3600) // 60):02d}:{t % 60:05.2f}"


def build(*, item: dict, item_id: str, hook: str, beats: list[float], dur: float, lay: dict,
          video_for_faces: Path | None, out_ass: Path, results_dir: Path | None = None) -> dict | None:
    """Write the ASS file; return {pattern, boxes, ass} (boxes in 1080x1920 coords) or None."""
    pat = pick_pattern(item_id, results_dir, forced=(item or {}).get("caption_pattern"))
    lines = story_lines(hook, (item or {}).get("title") or "", (item or {}).get("description") or "",
                        (item or {}).get("words") or "")
    groups = chunks(lines, int(pat["words"]))
    if not groups:
        return None
    slots = beat_slots(beats, dur, len(groups))
    events, boxes, k = [], [], 0
    for i, (grp, (t0, t1)) in enumerate(zip(groups, slots)):
        faces = detect_faces(video_for_faces, (t0 + t1) / 2, lay) if video_for_faces else []
        pl = place(grp, pat, lay, faces, i)
        if not pl:
            continue  # never cover a face: drop this beat
        x, y, w, h = pl["box"]
        parts = []
        for wd in grp:
            if is_key(wd):
                col = pat["keys"][k % len(pat["keys"])]
                k += 1
                parts.append(r"{\fs%d\c%s}%s" % (pl["key_px"], _ass_color(col), wd.upper()))
            else:
                parts.append(r"{\fs%d\c&H00FFFFFF&}%s" % (pl["fil_px"], wd.upper()))
        pos = r"{\an5\pos(%d,%d)\fscx120\fscy120\t(0,200,\fscx100\fscy100)\shad0}" % (x + w // 2, y + h // 2)
        body = " ".join(parts)
        events.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},Neon,,0,0,0,," + pos + r"{\bord8\blur12\1a&HFF&}" + body)
        events.append(f"Dialogue: 1,{_ts(t0)},{_ts(t1)},Neon,,0,0,0,," + pos + r"{\bord2\blur0.6}" + body)
        boxes.append({"t0": t0, "t1": t1, "box": [x, y, w, h], "text": " ".join(grp),
                      "faces": [list(f) for f in faces]})
    if not events:
        return None
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Neon,Playfair Display,84,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,2,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    Path(out_ass).write_text(hdr + "\n".join(events) + "\n", encoding="utf-8")
    return {"pattern": pat["id"], "pattern_name": pat["name"], "boxes": boxes, "ass": str(out_ass)}


def qa(boxes: list[dict], lay: dict) -> list[str]:
    """pro-edit QA: captions inside the picture safe area (2.5% margin) and clear of faces."""
    px, py, pw, ph = int(lay["plate_x"]), int(lay["plate_y"]), int(lay["plate_w"]), int(lay["plate_h"])
    bad = []
    for c in boxes:
        x, y, w, h = c["box"]
        if x < px + 0.02 * pw or x + w > px + pw * 0.98 or y < py or y + h > py + ph:
            bad.append(f"outside safe area: {c['text']}")
        if any(_overlap((x, y, w, h), tuple(f)) > 0.02 * w * h for f in c.get("faces") or []):
            bad.append(f"over a face: {c['text']}")
    return bad
