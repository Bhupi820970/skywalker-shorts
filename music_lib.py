"""Trending edit-song library picker (music/library.json; mp3s from the 'music' GitHub release)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import perf

ROOT = Path(__file__).resolve().parent
_DENY_CACHE: list[dict] | None = None


def _norm_music(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def denylist_entries(AE: Path | None = None) -> list[dict]:
    global _DENY_CACHE
    if _DENY_CACHE is not None:
        return _DENY_CACHE
    entries: list[dict] = []
    for base in (AE, ROOT):
        if not base:
            continue
        fp = Path(base) / "config" / "music_denylist.json"
        if not fp.exists():
            continue
        try:
            doc = json.loads(fp.read_text())
        except Exception:
            continue
        for e in doc.get("entries") or []:
            if isinstance(e, dict):
                entries.append(e)
        break
    _DENY_CACHE = entries
    return entries


_BLOCK_CACHE: dict | None = None


def blocklist(AE: Path | None = None) -> dict:
    """Content ID block-list (config/music_blocklist.json)."""
    global _BLOCK_CACHE
    if _BLOCK_CACHE is not None:
        return _BLOCK_CACHE
    doc: dict = {}
    for base in (AE, ROOT):
        if not base:
            continue
        fp = Path(base) / "config" / "music_blocklist.json"
        if fp.exists():
            try:
                doc = json.loads(fp.read_text())
            except Exception:
                doc = {}
            break
    _BLOCK_CACHE = doc
    return doc


def _norm_amp(s: str) -> str:
    return _norm_music((s or "").replace("&", " and "))


def _meta_blob(t: dict) -> str:
    return " ".join(str(t.get(k) or "") for k in (
        "artist", "title", "credit", "label", "channel", "uploader", "source_url", "album", "file",
    )).lower()


def is_claimed_track(t: dict | None, AE: Path | None = None) -> bool:
    """A song that already got a Content ID claim is never used again (priority or not)."""
    if not t:
        return False
    title = _norm_amp(t.get("title") or "")
    blob = _norm_amp(_meta_blob(t))
    for c in blocklist(AE).get("claimed") or []:
        ct = _norm_amp(c.get("title") or "")
        ca = _norm_amp(c.get("artist") or "")
        if c.get("file") and c["file"] == t.get("file"):
            return True
        if ct and (ct == title or (len(ct) >= 6 and ct in blob)) and (not ca or ca in blob):
            return True
    return False


def is_big_label_track(t: dict | None, AE: Path | None = None) -> bool:
    """WMG / Sony / UMG / T-Series and similar label songs (Content ID risk)."""
    if not t:
        return False
    doc = blocklist(AE)
    raw = _meta_blob(t)
    flat = _norm_music(raw)
    for pat in doc.get("label_patterns") or []:
        p = str(pat).lower().strip()
        if not p:
            continue
        if len(_norm_music(p)) <= 4:
            if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", raw):
                return True
        elif _norm_music(p) in flat:
            return True
    for a in doc.get("label_artists") or []:
        if _norm_amp(a) and _norm_amp(a) in _norm_amp(t.get("artist") or ""):
            return True
    fname = (t.get("file") or "").lower()
    title = _norm_amp(t.get("title") or "")
    for e in doc.get("label_songs") or []:
        if e.get("file") and e["file"].lower() == fname:
            return True
        et = _norm_amp(e.get("title") or "")
        if et and len(et) >= 5 and title.startswith(et):
            return True
    return False


def is_blocked_track(t: dict | None, AE: Path | None = None) -> bool:
    """Approved Content ID block-list: claimed songs always; big-label songs unless BiBi's priority song."""
    if not t:
        return False
    if is_claimed_track(t, AE):
        return True
    if is_big_label_track(t, AE) and not is_priority(t):
        return True
    return False


def credit_block(t: dict) -> str:
    """Description credit for the song: artist, title and the official link (+ license when known)."""
    artist = (t.get("artist") or "").strip() or "Unknown artist"
    title = (t.get("title") or "").strip() or (t.get("file") or "track")
    lines = [f"Song: {artist} - {title}"]
    link = (t.get("source_url") or t.get("source_page") or "").strip()
    if not link and (t.get("source") == "archive.org") and t.get("source_id"):
        link = f"https://archive.org/details/{t['source_id']}"
    old = (t.get("credit") or "").strip()
    if not link:
        m = re.search(r"https?://\S+", old)
        if m and "creativecommons.org" not in m.group(0):
            link = m.group(0).rstrip(").,")
    if link:
        lines.append(f"Official link: {link}")
    lic = (t.get("license_url") or t.get("license") or "").strip()
    if lic:
        lines.append(f"License: {lic}")
    # keep extra lines of an existing multi-line credit (e.g. "Music provided by NoCopyrightSounds")
    for extra in old.splitlines()[1:]:
        e = extra.strip()
        if e and e not in lines and not any(e.endswith(x) for x in (link, lic) if x):
            lines.append(e)
    return "\n".join(lines)


def is_denied_track(t: dict | None, AE: Path | None = None) -> bool:
    """True if this track is on the permanent music denylist or the Content ID block-list."""
    if not t:
        return False
    if is_blocked_track(t, AE):
        return True
    entries = denylist_entries(AE)
    if not entries:
        return False
    fname = (t.get("file") or "").lower()
    sid = (t.get("source_id") or "").lower()
    blob = _norm_music(" ".join([
        t.get("file") or "", t.get("source_id") or "",
        t.get("artist") or "", t.get("title") or "",
        t.get("credit") or "", t.get("source_file") or "",
    ]))
    for e in entries:
        ef = (e.get("file") or "").lower()
        es = (e.get("source_id") or "").lower()
        if ef and fname and ef == fname:
            return True
        if es and sid and es == sid:
            return True
        for key in ("artist", "title", "file", "source_id"):
            val = _norm_music(e.get(key) or "")
            if len(val) >= 8 and val in blob:
                return True
    return False

LIB_PILLAR_GENRE = {
    "anime": ("phonk", "rock", "dark"), "movie": ("phonk", "rock", "dark", "mass", "slowed"),
    "celeb": ("mass", "swag", "phonk", "punjabi"), "romance": ("romance", "slowed", "bollywood", "urdu", "pop"),
    "funny": ("funk", "dance", "phonk"),
}

CURATED_MARKERS = ("phonk", "funk", "slowed", "drift", "brazilian", "reverb", "sped")
LOVE_MARKERS = ("romance", "romantic", "love", "emotional", "sad")
_GENERIC_BEAT_NAMES = (
    "hip-hop beat", "hip hop beat", "type beat", "phonk hip-hop beat", "phonk beat",
)
_ACTION_MUSIC = ("phonk", "hardstyle", "dubstep", "hype")


def is_generic_cc_beat(t: dict) -> bool:
    """Jamendo / Creative Commons 'Phonk Hip-Hop Beat' instrumentals, not trending edit songs."""
    title = (t.get("title") or "").lower()
    credit = (t.get("credit") or "").lower()
    license_ = (t.get("license") or "").lower()
    sid = (t.get("source_id") or "").lower()
    if "jamendo" in sid:
        return True
    if any(w in title for w in _GENERIC_BEAT_NAMES):
        return True
    cc = "creativecommons.org" in license_ or "creativecommons.org" in credit
    if cc and "beat" in title and "phonk" in title:
        return True
    return False


def _track_blob(t: dict) -> str:
    return " ".join([
        t.get("genre") or "", t.get("mood") or "",
        " ".join(t.get("tags") or []), t.get("title") or "",
        " ".join(t.get("pillars") or []),
    ]).lower()



DATED_LOVE_MARKERS = (
    "classical", "symphony", "sonata", "concerto", "opera", "quartet",
    "schumann", "brahms", "beethoven", "mozart", "chopin", "wagner",
    "bach", "vivaldi", "debussy", "liszt", "tchaikovsky", "schubert",
    "string quartet", "piano sonata", "violin and piano", "tone poet",
    "four romantic pieces", "siegmund", "paul whiteman", "ragtime",
    "dixieland", "big band", "op. ", " gypsy love", "venetian love",
    "arab love song", "isn\'t it romantic", "isnt it romantic",
    "loc-jukebox", "jukebox-", "publicdomain/mark",
)


def is_dated_love_track(t: dict) -> bool:
    """Classical / vintage jazz 'love' rips: legal, but low priority for modern romance edits."""
    if (t.get("priority") or "").lower() == "low":
        return True
    blob = " ".join([
        _track_blob(t),
        t.get("artist") or "",
        t.get("credit") or "",
        t.get("file") or "",
    ]).lower()
    return any(m in blob for m in DATED_LOVE_MARKERS)


def is_love_track(t: dict) -> bool:
    """Romantic / sad / emotional beds — not phonk action tracks that happen to say 'love'."""
    blob = _track_blob(t)
    if any(w in blob for w in _ACTION_MUSIC):
        return False
    return any(w in blob for w in LOVE_MARKERS)


def is_curated_edit_song(t: dict, *, mood: str | None = None) -> bool:
    """Claim-safe trending phonk, drift phonk, slowed/sped-up edit songs.

    When mood is love/romance, emotional/romance tracks also count as curated
    so they are not dropped in favour of phonk.
    """
    if is_generic_cc_beat(t):
        return False
    blob = _track_blob(t)
    if (mood or "") in ("love", "romance") and is_love_track(t):
        return True
    return any(w in blob for w in CURATED_MARKERS)


# ---------------------------------------------------------------------------
# Genre categories + owner priority tracks
# ---------------------------------------------------------------------------
MUSIC_CATS = ("action", "love", "cinematic", "hiphop")
# Which song categories fit an edit format (first = best).
FORMAT_CATS = {
    "action": ("action", "hiphop"),
    "gta6": ("action", "hiphop"),
    "love": ("love",),
    "romance": ("love",),
    "cinematic": ("cinematic",),
    "hiphop": ("hiphop",),
}
PRIORITY_BONUS = 60.0
_HIPHOP = ("hip hop", "hip-hop", "hiphop", "rap", "trap", "drill", "boom bap")
_CINEMATIC = ("cinematic", "epic", "orchestral", "trailer", "score", "ambient", "soundtrack")


def track_cat(t: dict) -> str:
    """action | love | cinematic | hiphop. Explicit `cat` wins, else derived from tags/genre."""
    c = (t.get("cat") or "").strip().lower()
    if c in MUSIC_CATS:
        return c
    blob = _track_blob(t)
    if any(w in blob for w in ("phonk", "funk", "drift", "hardstyle", "dubstep")):
        return "action"
    if is_love_track(t):
        return "love"
    if re.search(r"\b(hip[ -]?hop|rap|trap|drill|boom bap)\b", blob):
        return "hiphop"
    if any(w in blob for w in _CINEMATIC):
        return "cinematic"
    return "action"


def valid_drop(t: dict, sec: float, tol: float = 1.5) -> float | None:
    """Accept a Gemini drop second only if it is near the track's analysed drop/highs."""
    pts = [float(x) for x in (t.get("highs") or []) if x is not None]
    if t.get("drop_t") is not None:
        pts.append(float(t["drop_t"]))
    dur = float(t.get("duration_s") or 0)
    if dur and not (0 <= sec < dur - 8):
        return None
    if not pts:
        return None
    best = min(pts, key=lambda x: abs(x - sec))
    return best if abs(best - sec) <= tol else None


def is_priority(t: dict) -> bool:
    return (t.get("priority") or "").strip().lower() == "high"


def forced_genre() -> str:
    g = (os.environ.get("SKY_MUSIC_GENRE") or "").strip().lower()
    return g if g in MUSIC_CATS else ""


def cats_for(mood: str | None) -> tuple[str, ...]:
    g = forced_genre()
    if g:
        return (g,)
    return FORMAT_CATS.get((mood or "action").strip().lower(), ("action", "hiphop"))


def curated_tracks(tracks: list[dict], *, mood: str | None = None) -> list[dict]:
    """Prefer the curated phonk/edit library; only if it is empty, allow the rest (incl. CC beats).

    Love mood prefers romance/emotional tracks first. Owner priority tracks of a
    fitting category are always kept. A forced genre (SKY_MUSIC_GENRE) narrows
    the pool to that category when it has tracks.
    """
    g = forced_genre()
    if g:
        only = [t for t in tracks if track_cat(t) == g]
        if only:
            return only
    want = cats_for(mood)
    prio = [t for t in tracks if is_priority(t) and track_cat(t) in want]
    if (mood or "") in ("love", "romance"):
        love = [t for t in tracks if is_love_track(t) and not is_generic_cc_beat(t)]
        modern = [t for t in love if not is_dated_love_track(t)]
        modern = prio + [t for t in modern if t not in prio]
        if modern:
            return modern
        if love:
            return love
    curated = [t for t in tracks if is_curated_edit_song(t, mood=mood)]
    for t in prio:
        if t not in curated:
            curated.append(t)
    return curated if curated else list(tracks)



def catalog(AE: Path, exclude: list[str] | None = None, mood: str | None = None) -> list[dict]:
    """Compact library rows for a Gemini prompt (no paths, no licenses).

    Owner priority tracks of a fitting category are listed first."""
    lp = AE / "music" / "library.json"
    if not lp.exists():
        return []
    try:
        lib = json.loads(lp.read_text())
    except Exception:
        return []
    excl = {x for x in (exclude or []) if x}
    out = []
    pool = curated_tracks([t for t in (lib.get("tracks") or []) if t.get("file") and not is_denied_track(t, AE)],
                          mood=mood)
    want = cats_for(mood) if (mood or forced_genre()) else MUSIC_CATS
    for t in pool:
        if t["file"] in excl:
            continue
        row = {k: t.get(k) for k in (
            "file", "title", "artist", "genre", "mood", "bpm", "energy", "pillars",
            "safety", "drop_t", "highs",
        )}
        row["cat"] = track_cat(t)
        row["priority"] = "high" if is_priority(t) else ""
        out.append(row)
    out.sort(key=lambda r: (0 if (r["priority"] and r["cat"] in want) else 1))
    return out


def pick(AE: Path, pillar: str, seed: str = "", exclude: list[str] | None = None, salt: str = "",
         prefer: str | None = None, prefer_alts: list[str] | None = None,
         text: str = "") -> dict | None:
    """Trending edit songs from music/library.json (mp3s fetched from the 'music' release into music/lib).

    If Gemini named a file (`prefer`, then `prefer_alts`) and it is in the library and not in
    the last-N exclude list, that track is used. Otherwise a deterministic hash pick among
    near-best scores (pillar/genre/market/trends). Same song never repeats within a 3-day window (results history).
    Returns None to fall back to credits.json.
    """
    import hashlib
    if os.environ.get("SKY_MUSIC_SOURCE", "library") != "library":
        return None
    lp = AE / "music" / "library.json"
    if not lp.exists():
        return None
    try:
        lib = json.loads(lp.read_text())
    except Exception:
        return None
    ldir = AE / (lib.get("dir") or "music/lib")
    tracks = [t for t in lib.get("tracks") or [] if t.get("file") and (ldir / t["file"]).exists() and not is_denied_track(t, AE)]
    if not tracks:
        print("  music library: no mp3s present locally; falling back to credits.json")
        return None
    mood = "action"
    try:
        import edit_format
        mood = edit_format.infer_format(pillar=pillar, text=text)
        if mood == "gta6":
            mood = "action"
    except Exception:
        if (pillar or "").lower() in ("romance", "love"):
            mood = "love"
    want = cats_for(mood)
    curated = curated_tracks(tracks, mood=mood)
    if curated and curated != tracks:
        tracks = curated
    elif not any(is_curated_edit_song(t, mood=mood) for t in tracks):
        print("  music library: curated phonk/edit tracks missing; allowing generic CC fallback")
    p = (pillar or "movie").lower()
    market = (os.environ.get("SKY_MARKET") or "").upper()
    excl = {x for x in (exclude or []) if x}
    picks = []
    try:
        tr = json.loads((AE / "trends" / "latest.json").read_text())
        picks = [str(x).lower() for x in (tr.get("song_picks") or [])]
    except Exception:
        pass
    indian = ("bollywood", "punjabi", "south", "hindi", "urdu")

    try:
        _perf = perf.load_perf(AE)
    except Exception:
        _perf = {}

    def score(t):
        sc = 0.0
        cat = track_cat(t)
        if cat in want:
            sc += 20 if cat == want[0] else 8
        elif mood == "love" or forced_genre():
            sc -= 40
        if is_priority(t) and cat in want:
            # Owner's hand-picked songs beat everything that fits the format.
            sc += PRIORITY_BONUS
        if p in (t.get("pillars") or []):
            sc += 30
        g = (t.get("genre") or "").lower()
        if any(w in g for w in LIB_PILLAR_GENRE.get(p, ())):
            sc += 10
        # Prefer phonk / drift / brazilian / slowed+reverb beat-drop tracks
        # (love/romance titles prefer mid-tempo emotional tracks instead).
        if mood == "love":
            if is_love_track(t):
                sc += 40
            if is_dated_love_track(t):
                sc -= 35
            if any(w in g or w in (t.get("mood") or "").lower() for w in _ACTION_MUSIC) and not is_love_track(t):
                sc -= 25
            bpm = float(t.get("bpm") or 0)
            if 70 <= bpm <= 130:
                sc += 10
            elif bpm > 150:
                sc -= 15
        elif any(w in g for w in ("phonk", "funk", "slowed", "drift", "brazilian")):
            sc += 14
            if is_love_track(t):
                sc -= 8
        # Prefer copyright-safer tracks; claim_risk only when nothing else fits
        safety = (t.get("safety") or "").lower()
        if safety == "safe":
            sc += 16
        elif safety == "claim_risk":
            sc -= 10
        is_in = any(w in g for w in indian)
        if market == "IN" and is_in:
            sc += 12
        if market and market != "IN" and is_in:
            sc -= 12
        # Owner rule: prefer phonk/edit songs, not NCS-style instrumentals even if one scored well.
        fn = (t.get("file") or "").lower()
        if fn.startswith("ncs_") or "nocopyrightsounds" in (t.get("credit") or "").lower():
            sc -= 20
        name = f"{t.get('title','')} {t.get('artist','')}".lower()
        if any(k and (k in name or name.split(' (')[0] in k) for k in picks):
            sc += 8
        if t.get("drop_t") is not None:
            sc += 3
        # Weight toward songs that historically earned views (with exploration).
        if _perf:
            try:
                sc += float(perf.song_bonus(_perf, t))
            except Exception:
                pass
        return sc

    pool = [t for t in tracks if t["file"] not in excl] or tracks
    by_file = {t["file"]: t for t in pool}

    def finish(t, how):
        t = dict(t)
        t["path"] = str(ldir / t["file"])
        t["library"] = True
        t["source"] = t.get("source") or "library"
        t["pillar"] = p
        t["credit"] = credit_block(t)
        t["rank"] = score(t)
        t["picked_by"] = how
        return t

    for how, name in [("gemini", prefer), * [("gemini_alt", a) for a in (prefer_alts or [])]]:
        if name and name in by_file:
            if track_cat(by_file[name]) not in want:
                print(f"  music library: {how} chose {name} but it is {track_cat(by_file[name])}, "
                      f"not {'/'.join(want)}; skipping")
                continue
            print(f"  music library: {how} chose {name}")
            return finish(by_file[name], how)

    ranked = sorted(((score(t), t) for t in pool), key=lambda x: -x[0])
    best = ranked[0][0]
    # Wider near-best band so exploration can escape a single winner.
    top = [t for sc, t in ranked if sc >= best - 10]
    h = int(hashlib.sha1((f"{salt}|{seed or p}|perf").encode()).hexdigest(), 16)
    return finish(top[h % len(top)], "hash")


MUSIC_COOLDOWN_DAYS = 3


def _parse_result_when(r: dict):
    """Best timestamp for a render result (slot first, then `at`)."""
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    slot = r.get("slot") or ""
    if slot:
        try:
            dt = datetime.fromisoformat(str(slot).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ist)
            return dt
        except Exception:
            pass
    at = str(r.get("at") or "").replace(" IST", "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(at, fmt)
            if fmt.endswith("Z"):
                dt = dt.replace(tzinfo=timezone.utc)
            elif dt.tzinfo is None:
                dt = dt.replace(tzinfo=ist)
            return dt
        except Exception:
            continue
    return None


def recent_music_files(results_dir, *, days: int = MUSIC_COOLDOWN_DAYS, now=None,
                       exclude_id: str | None = None) -> list[str]:
    """Music files used by done renders in the last `days` days (by slot/`at`)."""
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now = now or datetime.now(ist)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ist)
    cut = now - timedelta(days=float(days))
    files: list[str] = []
    rd = Path(results_dir)
    if not rd.exists():
        return []
    for pth in rd.glob("*.json"):
        try:
            r = json.loads(pth.read_text())
        except Exception:
            continue
        if exclude_id and r.get("id") == exclude_id:
            continue
        if r.get("status") != "done" or not r.get("music"):
            continue
        dt = _parse_result_when(r)
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ist)
        if dt >= cut:
            files.append(str(r["music"]))
    return list(dict.fromkeys(files))


def expand_same_song(AE: Path | None, files: list[str] | None) -> list[str]:
    """Add other library tracks that share a normalized title with a used file."""
    used = [f for f in (files or []) if f]
    out = list(dict.fromkeys(used))
    if not AE:
        return out
    lp = Path(AE) / "music" / "library.json"
    if not lp.exists():
        return out
    try:
        tracks = json.loads(lp.read_text()).get("tracks") or []
    except Exception:
        return out
    titles = set()
    for t in tracks:
        fn = t.get("file") or ""
        if fn in used:
            titles.add(_norm_music(t.get("title") or ""))
    titles.discard("")
    if not titles:
        return out
    for t in tracks:
        fn = t.get("file") or ""
        title = _norm_music(t.get("title") or "")
        if fn and title and title in titles and fn not in out:
            out.append(fn)
    return out


def recent_excludes(results_dir, AE: Path | None = None, *, days: int = MUSIC_COOLDOWN_DAYS,
                    now=None, exclude_id: str | None = None) -> list[str]:
    """Files the picker must skip: used in the last `days` days, plus same-title copies."""
    files = recent_music_files(results_dir, days=days, now=now, exclude_id=exclude_id)
    return expand_same_song(AE, files)

