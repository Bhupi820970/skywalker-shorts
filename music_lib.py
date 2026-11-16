"""Trending edit-song library picker (music/library.json; mp3s from the 'music' GitHub release)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import perf

LIB_PILLAR_GENRE = {
    "anime": ("phonk", "rock", "dark"), "movie": ("phonk", "rock", "dark", "mass", "slowed"),
    "celeb": ("mass", "swag", "phonk", "punjabi"), "romance": ("romance", "slowed", "bollywood", "urdu", "pop"),
    "funny": ("funk", "dance", "phonk"),
}

CURATED_MARKERS = ("phonk", "funk", "slowed", "drift", "brazilian", "reverb", "sped")
_GENERIC_BEAT_NAMES = (
    "hip-hop beat", "hip hop beat", "type beat", "phonk hip-hop beat", "phonk beat",
)


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


def is_curated_edit_song(t: dict) -> bool:
    """Claim-safe trending phonk, drift phonk, slowed/sped-up edit songs."""
    if is_generic_cc_beat(t):
        return False
    blob = " ".join([
        t.get("genre") or "", t.get("mood") or "",
        " ".join(t.get("tags") or []), t.get("title") or "",
    ]).lower()
    return any(w in blob for w in CURATED_MARKERS)


def curated_tracks(tracks: list[dict]) -> list[dict]:
    """Prefer the curated phonk/edit library; only if it is empty, allow the rest (incl. CC beats)."""
    curated = [t for t in tracks if is_curated_edit_song(t)]
    return curated if curated else list(tracks)



def catalog(AE: Path, exclude: list[str] | None = None) -> list[dict]:
    """Compact library rows for a Gemini prompt (no paths, no licenses)."""
    lp = AE / "music" / "library.json"
    if not lp.exists():
        return []
    try:
        lib = json.loads(lp.read_text())
    except Exception:
        return []
    excl = {x for x in (exclude or []) if x}
    out = []
    pool = curated_tracks([t for t in (lib.get("tracks") or []) if t.get("file")])
    for t in pool:
        if t["file"] in excl:
            continue
        out.append({k: t.get(k) for k in (
            "file", "title", "artist", "genre", "mood", "bpm", "energy", "pillars",
            "safety", "drop_t",
        )})
    return out


def pick(AE: Path, pillar: str, seed: str = "", exclude: list[str] | None = None, salt: str = "",
         prefer: str | None = None, prefer_alts: list[str] | None = None) -> dict | None:
    """Trending edit songs from music/library.json (mp3s fetched from the 'music' release into music/lib).

    If Gemini named a file (`prefer`, then `prefer_alts`) and it is in the library and not in
    the last-N exclude list, that track is used. Otherwise a deterministic hash pick among
    near-best scores (pillar/genre/market/trends). Same song never repeats within 8 videos.
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
    tracks = [t for t in lib.get("tracks") or [] if t.get("file") and (ldir / t["file"]).exists()]
    if not tracks:
        print("  music library: no mp3s present locally; falling back to credits.json")
        return None
    curated = [t for t in tracks if is_curated_edit_song(t)]
    if curated:
        tracks = curated
    else:
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
        if p in (t.get("pillars") or []):
            sc += 30
        g = (t.get("genre") or "").lower()
        if any(w in g for w in LIB_PILLAR_GENRE.get(p, ())):
            sc += 10
        # Prefer phonk / drift / brazilian / slowed+reverb beat-drop tracks
        if any(w in g for w in ("phonk", "funk", "slowed", "drift", "brazilian")):
            sc += 14
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
        t.setdefault("credit", f"Song: {t.get('artist')} - {t.get('title')}")
        t["rank"] = score(t)
        t["picked_by"] = how
        return t

    for how, name in [("gemini", prefer), * [("gemini_alt", a) for a in (prefer_alts or [])]]:
        if name and name in by_file:
            print(f"  music library: {how} chose {name}")
            return finish(by_file[name], how)

    ranked = sorted(((score(t), t) for t in pool), key=lambda x: -x[0])
    best = ranked[0][0]
    # Wider near-best band so exploration can escape a single winner.
    top = [t for sc, t in ranked if sc >= best - 10]
    h = int(hashlib.sha1((f"{salt}|{seed or p}|perf").encode()).hexdigest(), 16)
    return finish(top[h % len(top)], "hash")


