"""Riyio-style captions (approved v2, 27 Nov 2026):
- Playfair Display, ALL CAPS, coloured glow on key words, white fillers; words appear as spoken.
- Glow: two ASS layers (blurred colour halo under crisp text), pop 120 % -> 100 % in ~200 ms.
- Placed BESIDE the speaking character (side of the face with more empty space), never on a face,
  stable inside a shot (same side and Y for every line of that shot), inside the picture safe area.
  The white hook bar above the picture is untouched.
- Never cover source typography with a blur rectangle: use a clean source or crop burned-in subs out.
Only words a character SAYS (sky_lyrics guard). Written as an ASS file (libass, same burn path as before).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FONT_SANS = ROOT / "fonts" / "PlayfairDisplay.ttf"
FONT_SERIF = ROOT / "fonts" / "PlayfairDisplay.ttf"
SANS_NAME, SERIF_NAME = "Playfair Display", "Playfair Display"
FONT, FONT_NAME = FONT_SERIF, SERIF_NAME          # back-compat names
W, H = 1080, 1920
MAX_WORDS, MAX_CHARS, GAP_S = 5, 16, 0.35   # ref lines span ~35-60 % of the width
TAIL_S = 0.5

FILLER = set("""a an the to of and or but so is are am was were be been i you he she it we they me him her us them
my your his its our their this that these those in on at for with as by from up out do does did not no yes yeah oh
uh um huh hey just got get gonna gotta will would can could should there here what who how why when where if then
than all some im youre dont thats ill ive id""".split())

WHITE = (255, 255, 255)
RED, GOLD, PINK, BLUE, GREEN, ORANGE = (255, 46, 60), (255, 200, 40), (255, 92, 170), (70, 170, 255), (52, 211, 153), (255, 140, 40)
ROTATE = [BLUE, ORANGE, PINK, GREEN, GOLD, RED]
MEANING = [
    (GREEN, r"money|cash|rich|dollars?|paid|pay|price|deal|bank|millions?|billions?"),
    (RED, r"kill|dead|death|die|blood|war|fight|enemy|hate|destroy|end|never|fear|pain|revenge|monster|evil|run|gun"),
    (GOLD, r"king|queen|power|win|gold|god|strong|hero|best|legend|crown|rule|boss|champion|control"),
    (PINK, r"love|heart|miss|sorry|lost|cry|tears|alone|forget|together|kiss|beautiful|mother|father|family"),
    (BLUE, r"cold|winter|ice|calm|quiet|dark|night|sea|ocean|sky|dream|soul|ghost|time|remember"),
]
SIZE_TALK, SIZE_DRAMA = 80, 96          # v2: a bit bigger than the first riyio samples
SPACING_TALK, SPACING_DRAMA = 4, 2
GLOW_BORD, GLOW_BLUR, GLOW_ALPHA = 9, 14, 0x60
TYPE_CHAR_S = 0.045                      # dramatic lines: one letter every 45 ms (capped to the first 60 % of the line)
SIZE_FILLER, SIZE_KEY, SIZE_PUNCH = SIZE_TALK, SIZE_TALK, SIZE_DRAMA   # back-compat


COLOR_NAMES = {"gold": GOLD, "green": GREEN, "red": RED, "blue": BLUE, "pink": PINK, "orange": ORANGE,
               "white": WHITE}


def apply_caption_plan(words: list[dict], plan: list[dict] | None) -> int:
    """Gemini's per-line caption plan -> word annotations (cap_line, cap_reveal, cap_color). Words are matched by
    their source time (src_start) to the planned line's source window. Returns how many words got a plan."""
    n = 0
    for w in words or []:
        for k in ("cap_line", "cap_reveal", "cap_color"):
            w.pop(k, None)
    for pl in plan or []:
        try:
            a, b = float(pl["src"][0]), float(pl["src"][1])
        except Exception:
            continue
        reveal = pl.get("reveal") if pl.get("reveal") in ("pop", "typewriter") else "pop"
        acc = {str(x.get("word")): COLOR_NAMES.get(str(x.get("color"))) for x in pl.get("accent") or []
               if isinstance(x, dict) and COLOR_NAMES.get(str(x.get("color")))}
        for w in words or []:
            ss = w.get("src_start", w.get("s"))
            if ss is None or not (a - 0.15 <= float(ss) <= b + 0.15) or "cap_line" in w:
                continue
            w["cap_line"] = pl.get("line")
            w["cap_reveal"] = reveal
            w["cap_color"] = acc.get(base(w.get("word")))
            n += 1
    return n


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
                    or prev.get("cap_line") != w.get("cap_line")
                    or float(w["t0"]) - float(prev["t1"]) > GAP_S or re.search(r"[.,?!;:]$", clean(prev["word"]))):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def is_dramatic(g: list[dict], gi: int, is_last: bool) -> bool:
    """Serif typewriter look: the drop line, or a short (<= 3 words) line carrying a threat / power / cold word."""
    if is_last:
        return True
    if len(g) > 3:
        return False
    return any(accent_for(w["word"], -1) in (RED, GOLD, BLUE) and base(w["word"]) not in FILLER
               and _meaning(w["word"]) for w in g)


def _meaning(word: str):
    b = base(word)
    for col, rx in MEANING:
        if re.fullmatch(rf"({rx})(s|es|ed|ing|er)?", b):
            return col
    return None


def style_group(g: list[dict], gi: int, is_last: bool) -> list[dict]:
    """[{t, s, size, col, key, punch, drama, outline, glow}]. One size per line (refs); meaning words and the punch
    word (end of a sentence / the drop line) in the line's accent colour, the rest white with the accent glow.
    Lines Gemini planned (apply_caption_plan) use ITS reveal (typewriter = dramatic serif) and accent words /
    colours; the rules below only fill in lines it did not plan."""
    if any("cap_reveal" in w for w in g):
        return _style_planned(g, gi, is_last)
    drama = is_dramatic(g, gi, is_last)
    mean = next((_meaning(w["word"]) for w in reversed(g) if _meaning(w["word"])), None)
    glow = mean or ROTATE[gi % len(ROTATE)]
    out = []
    for k, w in enumerate(g):
        t = clean(w["word"]).upper().rstrip(",.")
        b = base(w["word"])
        last = k == len(g) - 1
        punch = last and len(b) >= 3 and b not in FILLER and bool(is_last or re.search(r"[.!?]$", clean(w["word"])))
        key = bool(_meaning(w["word"])) or punch
        col = (_meaning(w["word"]) or glow) if key else WHITE
        out.append({"t": t, "s": float(w["t0"]), "key": bool(key), "punch": bool(punch), "drama": drama,
                    "size": SIZE_DRAMA if drama else SIZE_TALK, "col": col, "glow": glow,
                    # ref1 "LOT OF MONEY": impact words outline-only (transparent fill, coloured edge)
                    "outline": bool(punch and not drama and col in (GOLD, RED, GREEN))})
    return out


def _style_planned(g: list[dict], gi: int, is_last: bool) -> list[dict]:
    drama = any(w.get("cap_reveal") == "typewriter" for w in g)
    accents = [w["cap_color"] for w in g if w.get("cap_color") and w["cap_color"] != WHITE]
    glow = accents[-1] if accents else (WHITE if not drama else ROTATE[gi % len(ROTATE)])
    out = []
    for k, w in enumerate(g):
        t = clean(w["word"]).upper().rstrip(",.")
        b = base(w["word"])
        col = w.get("cap_color") if w.get("cap_color") and b not in FILLER else None
        punch = k == len(g) - 1 and bool(is_last) and len(b) >= 3 and b not in FILLER
        key = col is not None
        out.append({"t": t, "s": float(w["t0"]), "key": key, "punch": punch, "drama": drama,
                    "size": SIZE_DRAMA if drama else SIZE_TALK, "col": col or WHITE, "glow": glow,
                    "outline": bool(key and punch and not drama and col in (GOLD, RED, GREEN)), "planned": True})
    return out


def _font(size: int, drama: bool = False):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(FONT_SERIF if drama else FONT_SANS), max(8, int(size)))
    except Exception:
        return ImageFont.load_default()


def measure(items: list[dict]) -> tuple[int, int]:
    wsum, hmax = 0, 0
    for it in items:
        drama = bool(it.get("drama"))
        f = _font(it["size"], drama)
        b = f.getbbox(it["t"] + " ")
        sp = SPACING_DRAMA if drama else SPACING_TALK
        wsum += (b[2] - b[0]) + sp * (len(it["t"]) + 1)
        hmax = max(hmax, b[3] - b[1])
    pad = 2 * GLOW_BORD + 8
    return int(wsum) + pad, int(hmax * 1.3) + pad      # + glow room


def _overlap(a, b) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))


def _hit_box(box) -> tuple:
    """Collision box without the glow pad, so the halo can sit close to a face without covering it."""
    x, y, w, h = box
    p = int(GLOW_BORD)
    return (x + p, y + p, max(1, w - 2 * p), max(1, h - 2 * p))


def _person_box(f) -> tuple:
    """Keep-out around a face (head + a little shoulder): type sits beside the speaker, not on them."""
    fx, fy, fw, fh = f
    return (fx - 0.30 * fw, fy - 0.35 * fh, fw * 1.60, fh * 1.70)


def place(tw: int, th: int, lay: dict, faces: list, locked: dict | None = None) -> tuple[tuple[int, int, int, int], str]:
    """Beside the biggest face, on the side with more empty space (or between two heads / in the sky above).
    Never over a face, clamped to the picture. With `locked` (same shot) reuse that side and Y so captions
    do not jump inside a shot."""
    px, py, pw, ph = int(lay["plate_x"]), int(lay["plate_y"]), int(lay["plate_w"]), int(lay["plate_h"])
    m = int(0.04 * pw)
    if tw > pw - 2 * m:
        tw = pw - 2 * m
    keep = [_person_box(f) for f in faces]

    def clamp(cx, cy):
        x0 = int(min(max(cx - tw / 2, px + m), px + pw - m - tw))
        y0 = int(min(max(cy - th / 2, py + m), py + ph - m - th))
        return (x0, y0, tw, th)

    def score(box, zone, prefer):
        hb = _hit_box(box)
        ov = sum(_overlap(hb, k) for k in keep) + 2 * sum(_overlap(hb, tuple(f)) for f in faces)
        pref = 0 if (prefer and zone == prefer) or zone == "between" else 80
        return (ov, pref, box[1])

    prefer, cands = None, []
    if faces:
        fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        fcx, fcy = fx + fw / 2, fy + fh / 2
        prefer = "right" if fcx < px + pw / 2 else "left"
        xs = sorted(faces, key=lambda f: f[0] + f[2] / 2)
        if len(xs) >= 2:
            for a_, b_ in zip(xs, xs[1:]):
                cands.append((((a_[0] + a_[2]) + b_[0]) / 2, min(a_[1], b_[1]) - th / 2 - 6, "between"))
        for sd in (prefer, "left" if prefer == "right" else "right"):
            for dy in (0.0, 0.15, -0.15, 0.40, -0.35):
                cx = fcx + 0.90 * fw + tw / 2 if sd == "right" else fcx - 0.90 * fw - tw / 2
                cands.append((cx, fcy + dy * fh, sd))
        cands.append((fcx, fy - 0.12 * fh - th / 2, "above"))
    cands += [(px + 0.28 * pw, py + 0.20 * ph, "left"), (px + 0.72 * pw, py + 0.20 * ph, "right"),
              (px + 0.28 * pw, py + 0.42 * ph, "left"), (px + 0.72 * pw, py + 0.42 * ph, "right")]
    if locked:
        prefer = locked.get("zone") or prefer
        cy, side = locked.get("cy"), locked.get("zone")
        if side in ("left", "right") and faces:
            fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            fcx = fx + fw / 2
            xx = fcx + 0.90 * fw + tw / 2 if side == "right" else fcx - 0.90 * fw - tw / 2
            cands.insert(0, (xx, cy if cy is not None else fy + fh / 2, side))
        elif cy is not None:
            cands.insert(0, (locked.get("cx", px + pw * 0.28), cy, side or "locked"))
    best = None
    for cx, cy, zone in cands:
        box = clamp(cx, cy)
        sc = score(box, zone, prefer)
        if best is None or sc < best[0]:
            best = (sc, box, zone)
        if sc[0] <= 0.02 * tw * th and (not prefer or zone in (prefer, "between") or locked):
            break
    return best[1], best[2]


def _ass_col(rgb) -> str:
    r, g, b = rgb
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600):d}:{int((t % 3600) // 60):02d}:{t % 60:05.2f}"


def _alpha(a: int) -> str:
    return f"&H{a:02X}&"


def ass_header() -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: RefSans,{SANS_NAME},{SIZE_TALK},&H00FFFFFF,&H000000FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,{SPACING_TALK},0,1,0,0,5,0,0,0,1
Style: RefSerif,{SERIF_NAME},{SIZE_DRAMA},&H00FFFFFF,&H000000FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,{SPACING_DRAMA},0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _units(items: list[dict], t0: float, t1: float) -> list[tuple[str, int, dict]]:
    """(text, reveal ms, item): talk lines reveal word by word at the spoken time; dramatic lines type letter by
    letter (ref2 "WINTER CAME"), finished within the first 60 % of the line."""
    out = []
    for it in items:
        dt = max(0, int(round((it["s"] - t0) * 1000)))
        if not it.get("drama"):
            out.append((it["t"], dt, it))
            continue
        step = min(TYPE_CHAR_S, 0.6 * max(0.2, t1 - t0) / max(1, sum(len(x["t"]) for x in items)))
        for k, ch in enumerate(it["t"]):
            out.append((ch, dt + int(round(k * step * 1000)), it))
    return out


def _layers(items: list[dict], t0: float, t1: float) -> tuple[str, str]:
    """(glow layer, crisp layer) override text. Glow = transparent fill + blurred accent border; crisp = the text."""
    glow_parts, crisp_parts = [], []
    units = _units(items, t0, t1)
    for i, (txt, dt, it) in enumerate(units):
        nxt_word = i + 1 < len(units) and units[i + 1][2] is not it
        sep = " " if (nxt_word or not it.get("drama")) and i + 1 < len(units) else ""
        if it.get("drama") and i + 1 < len(units) and units[i + 1][2] is it:
            sep = ""
        fade = (dt, dt + 70)
        g_col = it["col"] if it["key"] else it["glow"]
        ga = GLOW_ALPHA if (it["key"] or it.get("drama")) else 0x90
        g_rev = (r"\3a&HFF&\t(%d,%d,\3a%s)" % (fade[0], fade[1], _alpha(ga))) if dt > 0 else r"\3a" + _alpha(ga)
        glow_parts.append(r"{\1a&HFF&\3c%s%s}%s%s" % (_ass_col(g_col), g_rev, txt, sep))
        if it.get("outline"):
            c_rev = (r"\3a&HFF&\t(%d,%d,\3a&H00&)" % fade) if dt > 0 else r"\3a&H00&"
            crisp_parts.append(r"{\1a&HFF&\bord2\3c%s%s}%s%s" % (_ass_col(it["col"]), c_rev, txt, sep))
        else:
            c_rev = (r"\alpha&HFF&\t(%d,%d,\alpha&H00&)" % fade) if dt > 0 else r"\alpha&H00&"
            crisp_parts.append(r"{\bord0\c%s%s}%s%s" % (_ass_col(it["col"]), c_rev, txt, sep))
    return "".join(glow_parts), "".join(crisp_parts)


def _shot_key(t: float, shots: list | None) -> int:
    if not shots:
        return 0
    for i, (a, b) in enumerate(shots):
        if float(a) - 0.02 <= t < float(b) + 0.02:
            return i
    return -1


def build(words: list[dict], end_s: float, lay: dict, faces_at=None, out_ass: Path | None = None,
          shots: list | None = None) -> dict | None:
    """words: mapped dialogue words on the output timeline ({t0, t1, word}); end_s = dialogue act end.
    faces_at(t) -> [(x, y, w, h)] in 1080x1920 coords. shots: [(t0, t1), ...] keeps placement stable inside a
    shot. Returns {boxes, ass, text} or None."""
    groups = group_words(words, end_s)
    if not groups:
        return None
    events, boxes, locked = [], [], {}
    for gi, g in enumerate(groups):
        t0 = float(g[0]["t0"])
        nxt = float(groups[gi + 1][0]["t0"]) if gi + 1 < len(groups) else end_s
        t1 = min(nxt, float(g[-1]["t1"]) + TAIL_S, end_s)
        if t1 - t0 < 0.15:
            t1 = min(end_s, t0 + 0.15)
        if t1 <= t0:
            continue
        items = style_group(g, gi, is_last=gi == len(groups) - 1)
        sk = _shot_key((t0 + t1) / 2, shots)
        if sk in locked:
            faces = locked[sk]["faces"]
        else:
            faces = []
            if faces_at:
                try:
                    faces = list(faces_at((t0 + t1) / 2) or [])
                except Exception:
                    faces = []
        zone = "left"
        x = y = bw = bh = 0
        if faces:
            fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            mm = int(0.04 * int(lay["plate_w"]))
            room = max(fx - (int(lay["plate_x"]) + mm), (int(lay["plate_x"]) + int(lay["plate_w"]) - mm) - (fx + fw))
        else:
            room = int(lay["plate_w"] * 0.55)
        for sc in (1.0, 0.92, 0.84, 0.76, 0.68, 0.60, 0.52, 0.44, 0.38):
            items_s = [dict(it, size=max(28, int(it["size"] * sc))) for it in items]
            tw, th = measure(items_s)
            if tw > max(32, room - 12) and sc > 0.38:
                continue
            (x, y, bw, bh), zone = place(tw, th, lay, faces, locked=locked.get(sk))
            if all(_overlap(_hit_box((x, y, bw, bh)), tuple(f)) <= 0 for f in faces):
                items = items_s
                break
        else:
            items = items_s
        if sk not in locked:
            locked[sk] = {"zone": zone, "cy": y + bh / 2, "cx": x + bw / 2, "faces": faces}
        style = "RefSerif" if items[0].get("drama") else "RefSans"
        glow_txt, crisp_txt = _layers(items, t0, t1)
        pos = r"\an5\pos(%d,%d)\shad0\fscx120\fscy120\t(0,200,\fscx100\fscy100)\fad(40,90)" % (x + bw // 2, y + bh // 2)
        events.append(f"Dialogue: 0,{_ts(t0)},{_ts(t1)},{style},,0,0,0,,"
                      + "{" + pos + r"\bord%d\blur%d" % (GLOW_BORD, GLOW_BLUR) + "}" + glow_txt)
        events.append(f"Dialogue: 1,{_ts(t0)},{_ts(t1)},{style},,0,0,0,,"
                      + "{" + pos + r"\blur0.6" + "}" + crisp_txt)
        boxes.append({"t0": round(t0, 3), "t1": round(t1, 3), "box": [x, y, bw, bh], "zone": zone,
                      "text": " ".join(it["t"] for it in items), "faces": [list(f) for f in faces],
                      "style": style})
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
        if any(_overlap(_hit_box((x, y, w, h)), tuple(f)) > 0.02 * w * h for f in c.get("faces") or []):
            bad.append(f"over a face: {c['text']}")
    return bad
