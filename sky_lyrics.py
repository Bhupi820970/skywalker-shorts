"""Captions = what a character SAYS, never song lyrics (ported from riyio 0b4b8b8, BiBi Nov 24 2026).

Root cause of that demo: the dialogue window landed on a stretch of the source where the film's own soundtrack is
a sung song (Mockingjay "The Hanging Tree"), and whisper transcribed the sung lyrics as if they were dialogue.
These helpers decide whether a transcribed window is real spoken dialogue and strip lyric / out-of-window words.
"""
from __future__ import annotations
import re

LYRIC_MARK = re.compile(r"[\u266a\u266b\u266c\u266f]|\b(singing|sings|vocali[sz]ing|humming|lyrics|chanting)\b", re.I)
STOP = set("""a an the to of and or but so is are am was were be been i you he she it we they me him her us them my
your his its our their this that these those in on at for with as by from up out do does did not no yes yeah oh uh um
just got get will would can could should there here what who how why when where if then than all im youre dont its
thats ill ive id""".split())

QUOTE_MIN_OVERLAP = 0.30     # share of Gemini's quoted dialogue words that whisper must also hear
REPEAT_MAX = 0.30            # share of words inside immediately repeated 2-4 word phrases ("are you, are you")
SPAN_MAX = 0.40              # share of words inside a span Gemini marked as sung vocals


def tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9' ]", " ", (text or "").lower().replace("\u2019", "'")).replace("'", "").split() if t]


def content(toks: list[str]) -> list[str]:
    return [t for t in toks if t not in STOP and len(t) > 1]


def words_text(words: list[dict]) -> str:
    return " ".join(str(w.get("word", "")) for w in words)


def quote_overlap(words: list[dict], quote: str) -> float | None:
    """Share of the quote's content words that whisper also heard. None = no usable quote."""
    q = set(content(tokens(quote)))
    if len(q) < 3:
        return None
    heard = set(content(tokens(words_text(words))))
    return len(q & heard) / len(q)


def repetition_score(words: list[dict]) -> float:
    """Share of words that sit in a 2-4 word phrase repeated right after itself (sung hooks repeat; talk rarely)."""
    t = tokens(words_text(words))
    hit = [False] * len(t)
    for n in (2, 3, 4):
        for i in range(len(t) - 2 * n + 1):
            if t[i:i + n] == t[i + n:i + 2 * n] and len(set(t[i:i + n])) > 1:   # "no no no no" is talk
                for k in range(i, i + 2 * n):
                    hit[k] = True
    # the same line sung again a bit later (chorus): 4+ word phrase seen twice
    for i in range(len(t) - 3):
        g = t[i:i + 4]
        for j in range(i + 4, len(t) - 3):
            if t[j:j + 4] == g:
                for k in list(range(i, i + 4)) + list(range(j, j + 4)):
                    hit[k] = True
    return (sum(hit) / len(t)) if t else 0.0


def clean_spans(spans) -> list[tuple[float, float]]:
    out = []
    for sp in spans or []:
        try:
            a, b = (float(sp[0]), float(sp[1])) if isinstance(sp, (list, tuple)) else (float(sp["start"]), float(sp["end"]))
        except Exception:
            continue
        if b > a:
            out.append((a, b))
    return out


def in_spans(t: float, spans: list[tuple[float, float]]) -> bool:
    return any(a - 0.2 <= t <= b + 0.2 for a, b in spans)


def lyric_reasons(words: list[dict], window_start: float = 0.0, quote: str = "",
                  song_spans=None, check_quote: bool = True) -> list[str]:
    """Why this transcribed window looks like song lyrics / not the character's dialogue ([] = real dialogue).
    words: whisper words relative to window_start; song_spans: absolute source seconds of sung vocals."""
    if not words:
        return []
    why = []
    marks = sum(1 for w in words if LYRIC_MARK.search(str(w.get("word", ""))))
    if marks:
        why.append(f"whisper marked {marks} word(s) as music / singing")
    spans = clean_spans(song_spans)
    if spans:
        inside = sum(1 for w in words if in_spans(window_start + (w["s"] + w["e"]) / 2, spans))
        if inside / len(words) >= SPAN_MAX:
            why.append(f"{inside}/{len(words)} words inside the source's sung-song part")
    rep = repetition_score(words)
    if rep >= REPEAT_MAX and len(words) >= 6:
        why.append(f"repeated lyric-like phrases ({rep:.0%} of words)")
    if check_quote:
        ov = quote_overlap(words, quote)
        if ov is not None and ov < QUOTE_MIN_OVERLAP:
            why.append(f"words do not match the scene's spoken lines (overlap {ov:.0%})")
    return why


def song_ngrams(song_meta: dict | None) -> set[tuple[str, ...]]:
    """2-gram+ phrases of the chosen song's title / lyrics (catalog 'title' and optional 'lyrics')."""
    out: set[tuple[str, ...]] = set()
    if not song_meta:
        return out
    for field in ("title", "lyrics"):
        t = tokens(str(song_meta.get(field) or ""))
        for n in (2, 3):
            for i in range(len(t) - n + 1):
                g = tuple(t[i:i + n])
                if content(list(g)):
                    out.add(g)
    return out


def filter_words(words: list[dict], end_s: float, window_start: float = 0.0, song_spans=None,
                 song_meta: dict | None = None) -> tuple[list[dict], list[str]]:
    """Keep only words that can be a character's line: inside the dialogue section (0..end_s), not inside a sung
    span, not music-marked, not part of a 2-3 word run that matches the chosen song's title / lyrics."""
    spans = clean_spans(song_spans)
    grams = song_ngrams(song_meta)
    toks = [(tokens(str(w.get("word", ""))) or [""])[0] for w in words]
    drop = set()
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) in grams:
                drop.update(range(i, i + n))
    kept, dropped = [], []
    for i, w in enumerate(words):
        reason = ""
        if w["s"] < -0.05 or w["e"] > end_s + 0.05:
            reason = "outside dialogue"
        elif LYRIC_MARK.search(str(w.get("word", ""))):
            reason = "music mark"
        elif spans and in_spans(window_start + (w["s"] + w["e"]) / 2, spans):
            reason = "sung part"
        elif i in drop:
            reason = "song title/lyrics"
        if reason:
            dropped.append(f"{w.get('word')}({reason})")
        else:
            kept.append(w)
    return kept, dropped


def captions_in_window(events: list[dict], end_s: float) -> list[dict]:
    """Caption events never start before the dialogue or run into the montage (times relative to dialogue start)."""
    out = []
    for ev in events:
        if ev.get("start", 0) >= end_s or ev.get("end", 0) <= 0:
            continue
        out.append({**ev, "start": max(0.0, ev["start"]), "end": min(end_s, ev["end"])})
    return out
