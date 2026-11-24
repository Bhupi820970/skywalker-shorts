"""Reference-style dialogue captions for Skywalker dialogue edits (ported from riyio 0b4b8b8, BiBi Nov 24 2026).

Look (ref1 / ref2): Playfair Display serif, white text with a coloured glow, pop-in 120% -> 100%, 1-4 words at a time
synced to the spoken words, key words bigger in an accent colour picked by meaning (threat = red, power = gold,
love / loss = pink, cold / calm = blue), the punch word (end of a line / the drop line) biggest. Placed centred under
the speaker's chin (YuNet face), never over a face, mid-picture when no face is visible, always inside the movie
picture (the white hook bar above it is untouched). Only words a character SAYS (sky_lyrics guard: no sung part,
no song title / lyrics, nothing outside the dialogue act).
Written as an ASS file (libass, same burn path as the old lower-thirds).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FONT = ROOT / "fonts" / "PlayfairDisplay.ttf"
FONT_NAME = "Playfair Display"
W, H = 1080, 1920
MAX_WORDS, MAX_CHARS, GAP_S = 4, 18, 0.35
TAIL_S = 0.5

FILLER = set("""a an the to of and or but so is are am was were be been i you he she it we they me him her us them
my your his its our their this that these those in on at for with as by from up out do does did not no yes yeah oh
uh um huh hey just got get gonna gotta will would can could should there here what who how why when where if then
than all some im youre dont thats ill ive id""".split())

WHITE = (255, 255, 255)
RED, GOLD, PINK, BLUE, GREEN, ORANGE = (255, 46, 60), (255, 200, 40), (255, 92, 170), (70, 170, 255), (52, 211, 153), (255, 140, 40)
ROTATE = [BLUE, ORANGE, PINK, GREEN, GOLD, RED]
MEANING = [
    (RED, r"kill|dead|death|die|blood|war|fight|enemy|hate|destroy|end|never|fear|pain|revenge|monster|evil|run|gun"),
    (GOLD, r"king|queen|power|win|gold|money|rich|god|strong|hero|best|legend|crown|rule|boss|champion"),
    (PINK, r"love|heart|miss|sorry|lost|cry|tears|alone|forget|together|kiss|beautiful|mother|father|family"),
    (BLUE, r"cold|winter|ice|calm|quiet|dark|night|sea|ocean|sky|dream|soul|ghost|time|remember"),
]
SIZE_FILLER, SIZE_KEY, SIZE_PUNCH = 46, 66, 88     # px on the 1080x1920 frame (ref caption cap height ~4-6%)


def clean(w: str) -> str:
    return re.sub(r"[^A-Za-z0-9'?!.,-]", "", str(w or "")).strip()


def base(w: str) -> str:
    return re.sub(r"[^a-z']", "", clean(w).lower()).replace("'", "")


def accent_for(word: str, i: int) -> tuple[int, int, int]:
    b = base(word)
    for col, rx in MEANING:
        if re.fullmatch(rf"({rx})(s|es|ed|ing|er)?", b):
            return col
    return ROTATE[i % len(ROTATE)]


def group_words(words: list[dict], end_s: float) -> list[list[dict]]:
    """1-4 word groups on output time (t0/t1); split on pauses, punctuation and length."""
    groups, cur = [], []
    for w in words:
        if float(w["t0"]) >= end_s or not clean(w.get("word")):
            continue
        if cur:
            prev = cur[-1]
            chars = sum(len(clean(x["word"])) for x in cur)
            if (len(cur) >= MAX_WORDS or chars + len(clean(w["word"])) > MAX_CHARS
                    or float(w["t0"]) - float(prev["t1"]) > GAP_S or re.search(r"[.,?!;:]$", clean(prev["word"]))):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def style_group(g: list[dict], gi: int, is_last: bool) -> list[dict]:
    """[{t, size, col, key, punch}] - punch = last word of a sentence / the drop line (if it is a real word)."""
    out = []
    for k, w in enumerate(g):
        t = clean(w["word"]).upper().rstrip(",.")
        b = base(w["word"])
        last = k == len(g) - 1
        punch = last and len(b) >= 3 and b not in FILLER and (is_last or re.search(r"[.!?]$", clean(w["word"])))
        key = punch or (b not in FILLER and len(b) >= 3)
        col = RED if punch and accent_for(w["word"], gi) == ROTATE[gi % len(ROTATE)] else accent_for(w["word"], gi)
        out.append({"t": t, "s": float(w["t0"]), "key": bool(key), "punch": bool(punch),
                    "size": SIZE_PUNCH if punch else (SIZE_KEY if key else SIZE_FILLER),
                    "col": col if key else WHITE, "glow": accent_for(g[-1]["word"], gi)})
    return out


def _font(size: int):
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(str(FONT), max(8, int(size)))
        try:
            f.set_variation_by_axes([800])
        except Exception:
            pass
        return f
    except Exception:
        return ImageFont.load_default()


def measure(items: list[dict]) -> tuple[int, int]:
    wsum, hmax = 0, 0
    for it in items:
        b = _font(it["size"]).getbbox(it["t"] + " ")
        wsum += b[2] - b[0]
        hmax = max(hmax, b[3] - b[1])
    return int(wsum) + 24, int(hmax * 1.35) + 16      # + glow room


def _overlap(a, b) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))


def place(tw: int, th: int, lay: dict, faces: list) -> tuple[tuple[int, int, int, int], str]:
    """Centred under the biggest face's chin; mid-picture (60%) without a face; never over a face."""
    px, py, pw, ph = int(lay["plate_x"]), int(lay["plate_y"]), int(lay["plate_w"]), int(lay["plate_h"])
    m = int(0.03 * pw)
    if tw > pw - 2 * m:
        tw = pw - 2 * m
    cands = []
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        cx = fx + fw / 2
        cands.append((cx, fy + fh + 0.12 * fh + th / 2, "chin"))
        cands.append((px + pw / 2, py + 0.80 * ph, "low"))
        cands.append((cx, fy - 0.10 * fh - th / 2, "above"))
    cands.append((px + pw / 2, py + 0.60 * ph, "mid"))
    best = None
    for cx, cy, zone in cands:
        x0 = int(min(max(cx - tw / 2, px + m), px + pw - m - tw))
        y0 = int(min(max(cy - th / 2, py + m), py + ph - m - th))
        box = (x0, y0, tw, th)
        ov = sum(_overlap(box, tuple(f)) for f in faces)
        if best is None or ov < best[2]:
            best = (box, zone, ov)
        if ov <= 0.02 * tw * th:
            break
    return best[0], best[1]


def _ass_col(rgb) -> str:
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600):d}:{int((t % 3600) // 60):02d}:{t % 60:05.2f}"


def ass_header() -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: RefGlow,{FONT_NAME},64,&H00FFFFFF,&H000000FF,&H00FF9F46,&H00000000,-1,0,0,0,100,100,1,0,1,4,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build(words: list[dict], end_s: float, lay: dict, faces_at=None, out_ass: Path | None = None) -> dict | None:
    """words: mapped dialogue words on the output timeline ({t0, t1, word}); end_s = dialogue act end.
    faces_at(t) -> [(x, y, w, h)] in 1080x1920 coords. Returns {boxes, ass, text} or None."""
    groups = group_words(words, end_s)
    if not groups:
        return None
    events, boxes = [], []
    for gi, g in enumerate(groups):
        t0 = float(g[0]["t0"])
        nxt = float(groups[gi + 1][0]["t0"]) if gi + 1 < len(groups) else end_s
        t1 = min(nxt, float(g[-1]["t1"]) + TAIL_S, end_s)
        if t1 - t0 < 0.15:
            t1 = min(end_s, t0 + 0.15)
        if t1 <= t0:
            continue
        items = style_group(g, gi, is_last=gi == len(groups) - 1)
        tw, th = measure(items)
        faces = []
        if faces_at:
            try:
                faces = list(faces_at((t0 + t1) / 2) or [])
            except Exception:
                faces = []
        (x, y, bw, bh), zone = place(tw, th, lay, faces)
        glow = items[-1]["glow"]
        parts = []
        for it in items:
            # word-by-word reveal: each word appears when it is spoken (alpha jump at its start time)
            dt = max(0, int(round((it["s"] - t0) * 1000)))
            reveal = r"\alpha&HFF&\t(%d,%d,\alpha&H00&)" % (dt, dt + 1) if dt > 0 else ""
            # white words glow in the line's accent colour; accent-coloured key words get a dark soft edge so the
            # fill never melts into a same-colour glow
            edge = glow if not it["key"] else (20, 12, 10)
            parts.append(r"{\fs%d\c%s\3c%s%s}%s" % (it["size"], _ass_col(it["col"]), _ass_col(edge), reveal, it["t"]))
        head = (r"{\an5\pos(%d,%d)\bord3\blur7\shad0\fscx120\fscy120\t(0,200,\fscx100\fscy100)\fad(40,90)}"
                % (x + bw // 2, y + bh // 2))
        events.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},RefGlow,,0,0,0,,{head}" + " ".join(parts))
        boxes.append({"t0": round(t0, 3), "t1": round(t1, 3), "box": [x, y, bw, bh], "zone": zone,
                      "text": " ".join(it["t"] for it in items), "faces": [list(f) for f in faces]})
    if not events:
        return None
    if out_ass:
        Path(out_ass).write_text(ass_header() + "\n".join(events) + "\n", encoding="utf-8")
    return {"boxes": boxes, "ass": str(out_ass) if out_ass else "", "text": " / ".join(b["text"] for b in boxes)}


def qa(boxes: list[dict], lay: dict) -> list[str]:
    """Inside the picture, clear of faces, never above the plate (the hook bar)."""
    px, py, pw, ph = int(lay["plate_x"]), int(lay["plate_y"]), int(lay["plate_w"]), int(lay["plate_h"])
    bad = []
    for c in boxes:
        x, y, w, h = c["box"]
        if x < px or x + w > px + pw or y < py or y + h > py + ph:
            bad.append(f"outside the picture: {c['text']}")
        if any(_overlap((x, y, w, h), tuple(f)) > 0.02 * w * h for f in c.get("faces") or []):
            bad.append(f"over a face: {c['text']}")
    return bad
