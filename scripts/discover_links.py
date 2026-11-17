#!/usr/bin/env python3
"""Find official trending scene-clip links and queue them for the link pipeline.

A scheduled GitHub job (and the same script on the box) uses one Gemini call,
then yt-dlp ytsearch plus official X account posts, to find best-scene clips.
Only studio/distributor channels are accepted. Trailers are skipped. Each
candidate is sampled and passed through overlay_guard; a corner logo or
persistent bug means we skip that clip and try another OFFICIAL clip of the
same title. Unofficial re-uploads are never used to dodge a logo. Nothing is
blurred, cropped, or covered. Accepted links go into the queue and
links/requests.json so the normal link mode renders and schedules them.

GitHub runners often cannot download YouTube (bot check). In that case the
job records the official candidates and prints the box command, which
downloads a short sample only (no render, no cookies).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import overlay_guard  # noqa: E402
import sourcing  # noqa: E402
import submit_link  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
STATUS = "config/discover_status.json"
TRAILER = re.compile(r"\b(trailer|teaser|promo|tv\s*spot)\b", re.I)
JUNK = re.compile(
    r"(#shorts|\bedit\b|amv|status|whatsapp|compilation|scene pack|clip pack|"
    r"hindi dubbed|slowed|reverb|lyrics|reaction|explained|recap|review|"
    r"full\s*movie|camrip|hdcam)",
    re.I,
)
BOT = re.compile(r"sign in to confirm|not a bot|use cookies to|confirm your.{0,12}age", re.I)
# Official studios/distributors beyond (and including) the catalog list.
# Crunchyroll / Funimation / HIDIVE are official but their YouTube uploads
# usually burn a logo — they stay official so we never swap in a fan reupload.
EXTRA_OFFICIAL = (
    "Aniplex", "Aniplex USA", "ANIPLEX.EXE", "TOHO animation", "TOHO",
    "Warner Bros. Pictures", "Warner Bros.", "Marvel Entertainment",
    "Sony Pictures Entertainment", "Prime Video", "Amazon MGM Studios",
    "Netflix", "Netflix Anime", "Netflix India", "Walt Disney Studios",
    "Disney+", "Pixar", "DC", "Universal Pictures", "Paramount Pictures",
    "Lionsgate Movies", "A24", "Apple TV", "T-Series", "YRF",
    "Dharma Productions", "Hombale Films", "MAPPA", "WIT STUDIO",
    "Crunchyroll", "Funimation", "HIDIVE",
    "Rockstar Games",
)
# Handle (no @) -> display name. Shared with sourcing (link-mode X-first path).
OFFICIAL_X_HANDLES = dict(sourcing.OFFICIAL_X_HANDLES)
# Official YouTube channels scanned daily for new episode/movie clips (last 24h).
OFFICIAL_YT_CHANNELS = {
    "Netflix": "https://www.youtube.com/@Netflix",
    "Netflix Anime": "https://www.youtube.com/@NetflixAnime",
    "Netflix India": "https://www.youtube.com/@NetflixIndia",
    "Prime Video": "https://www.youtube.com/@PrimeVideo",
    "Warner Bros. Pictures": "https://www.youtube.com/@WarnerBrosPictures",
    "Marvel Entertainment": "https://www.youtube.com/@marvel",
    "Sony Pictures Entertainment": "https://www.youtube.com/@sonypictures",
    "TOHO animation": "https://www.youtube.com/@TOHOanimation",
    "Aniplex USA": "https://www.youtube.com/@AniplexUS",
    "Walt Disney Studios": "https://www.youtube.com/@DisneyStudios",
    "Pixar": "https://www.youtube.com/@Pixar",
    "Universal Pictures": "https://www.youtube.com/@UniversalPictures",
    "Paramount Pictures": "https://www.youtube.com/@ParamountPictures",
    "Lionsgate Movies": "https://www.youtube.com/@LionsgateMovies",
    "A24": "https://www.youtube.com/@A24",
    "Crunchyroll": "https://www.youtube.com/@Crunchyroll",
    "Adult Swim": "https://www.youtube.com/@adultswim",
    "Rockstar Games": "https://www.youtube.com/@RockstarGames",
}
NEW_RELEASE = re.compile(
    r"\b(episode|ep\.?\s*\d|new episode|now streaming|now playing|"
    r"in theaters|is out|premieres?|official clip|scene|watch now)\b",
    re.I,
)
KNOWN_SUFFIXES = (
    "official", "animation", "pictures", "entertainment", "india", "anime",
    "uk", "plus", "studios", "films", "movies", "tv", "usa", "channel", "exe",
)
MAX_TITLES = 6
MAX_SAMPLE_PER_TITLE = 4
DEFAULT_MAX_ACCEPT = 2


def log(msg: str) -> None:
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST discover: {msg}", flush=True)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def official_names(root: Path | None = None) -> set[str]:
    root = root or REPO
    names = {norm(x) for x in EXTRA_OFFICIAL if norm(x)}
    names |= {norm(h) for h in OFFICIAL_X_HANDLES}
    names |= {norm(v) for v in OFFICIAL_X_HANDLES.values()}
    try:
        cat = json.loads((root / "config" / "source_catalog.json").read_text())
        for lst in (cat.get("official_channels") or {}).values():
            names |= {norm(c) for c in (lst or []) if norm(c)}
    except Exception:
        pass
    return {n for n in names if n}


def is_known_official(channel: str, handle: str = "", official: set[str] | None = None) -> bool:
    """Strict official-studio match. A 'verified' badge alone is not enough."""
    official = official or official_names()
    for raw in (channel, handle):
        n = norm(raw)
        if not n:
            continue
        if n in official:
            return True
        for c in official:
            if len(c) < 4:
                continue
            if n.startswith(c):
                rest = n[len(c):]
                if not rest or any(rest == s or rest.startswith(s) for s in KNOWN_SUFFIXES):
                    return True
    return False


def is_stamped_channel(name: str) -> bool:
    """Official channels known to burn a logo/subs. Skip, don't replace with fans."""
    return bool(
        sourcing.is_logo_burn_channel(name)
        or overlay_guard.channel_blocked(name)
    )


def is_trailer_text(*parts: str) -> bool:
    blob = " ".join(p or "" for p in parts)
    return bool(TRAILER.search(blob) or sourcing.is_trailer_source(blob))


def denied_ids(root: Path | None = None) -> set[str]:
    root = root or REPO
    ids: set[str] = set()
    try:
        for x in json.loads((root / "config" / "clip_denylist.json").read_text()).get("ids") or []:
            s = str(x).strip().lower()
            if not s:
                continue
            ids.add(s)
            bare = s.split(":", 1)[-1]
            if bare:
                ids.add(bare)
                ids.add(f"yt:{bare}")
    except Exception:
        pass
    return ids


def is_denied(cid: str, url: str, deny: set[str]) -> bool:
    for token in (cid, url):
        s = (token or "").strip().lower()
        if not s:
            continue
        if s in deny:
            return True
        bare = s.split(":", 1)[-1]
        if bare in deny or f"yt:{bare}" in deny:
            return True
        m = re.search(r"(?:v=|/youtu\.be/|/shorts/|/status/)([A-Za-z0-9_-]{6,})", s)
        if m and (m.group(1).lower() in deny or f"yt:{m.group(1).lower()}" in deny):
            return True
    return False


def recent_titles(root: Path, days: int = 7) -> set[str]:
    cut = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
    used = set()
    for folder in ("queue", "results"):
        for p in (root / folder).glob("*.json"):
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            rows = data if isinstance(data, list) else [data]
            for it in rows:
                if not isinstance(it, dict):
                    continue
                slot = str(it.get("slot") or p.stem)[:10]
                if slot and slot < cut and folder == "queue" and p.stem[:4].isdigit():
                    if p.stem < cut:
                        continue
                words = str(it.get("words") or "")
                if words:
                    used.add(words.lower())
                parts = (it.get("id") or "").split("-")
                if len(parts) >= 3 and parts[0].isdigit():
                    used.add("-".join(parts[2:]).lower())
    try:
        for r in (json.loads((root / "links" / "requests.json").read_text()).get("requests") or []):
            for k in ("title", "slug", "words"):
                if r.get(k):
                    used.add(str(r[k]).lower())
            if r.get("url"):
                used.add(str(r["url"]).lower())
    except Exception:
        pass
    return used


def already_have_url(root: Path, url: str) -> bool:
    u = submit_link.normalize_url(url).lower()
    if not u:
        return False
    try:
        for r in (json.loads((root / "links" / "requests.json").read_text()).get("requests") or []):
            if submit_link.normalize_url(r.get("url") or "").lower() == u:
                return True
    except Exception:
        pass
    for folder in ("queue", "results"):
        for p in (root / folder).glob("*.json"):
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            rows = data if isinstance(data, list) else [data]
            for it in rows:
                if not isinstance(it, dict):
                    continue
                for k in ("source_url", "url"):
                    if submit_link.normalize_url(str(it.get(k) or "")).lower() == u:
                        return True
                for alt in it.get("alt_source_urls") or []:
                    if submit_link.normalize_url(str(alt)).lower() == u:
                        return True
    return False


def fallback_topics(root: Path, used: set[str]) -> list[dict]:
    out, seen = [], set()
    paths = [root / "config" / "clip_requests.json", root / "trends" / "latest.json"]
    blobs: list[dict] = []
    for p in paths:
        try:
            blobs.append(json.loads(p.read_text()))
        except Exception:
            continue
    rows = []
    for doc in blobs:
        rows.extend(doc.get("requests") or [])
        for key in ("topics", "picks", "titles"):
            val = doc.get(key)
            if isinstance(val, list):
                rows.extend(val)
    for r in rows:
        if not isinstance(r, dict):
            continue
        words = str(r.get("words") or r.get("title") or r.get("slug") or "").strip()
        slug = norm(r.get("slug") or words)[:24]
        if not words or not slug or slug in seen:
            continue
        if slug in used or words.lower() in used:
            continue
        seen.add(slug)
        out.append({
            "slug": slug,
            "words": words,
            "hook": r.get("hook") or "",
            "title": r.get("title") or words,
            "official_channels": list(r.get("official_channels") or []),
            "search_queries": [q for q in (r.get("search_queries") or []) if q and not TRAILER.search(q)],
            "x_handles": list(r.get("x_handles") or []),
            "x_urls": list(r.get("x_urls") or []),
        })
        if len(out) >= MAX_TITLES:
            break
    return out


def gemini_topics(root: Path, used: set[str]) -> list[dict]:
    os.environ.setdefault("SKY_GEMINI_BUDGET_FILE", "/tmp/gemini_discover_budget.json")
    os.environ.setdefault("GEMINI_MAX_CALLS", "1")
    try:
        import gemini
    except Exception:
        return []
    official = sorted(official_names(root))[:80]
    gta_note = ""
    try:
        import edit_format
        if edit_format.gta6_has_launched():
            gta_note = (
                " GTA 6 HAS LAUNCHED: include 2 official Rockstar Games GAMEPLAY clips "
                "(never trailers or teasers; official Rockstar watermark is ok)."
            )
        else:
            gta_note = " Do NOT pick GTA 6 / Grand Theft Auto VI until the official launch."
        gta_note += " Also include 2-3 official ROMANCE/love anime or movie titles (Netflix, TOHO, Lionsgate, Sony, Prime, Warner, GKIDS) for a slower love-edit format. Only romantic or cute moments — never sexual, explicit, nude, ecchi, or hentai clips."
    except Exception:
        gta_note = " Prefer currently trending official titles. Include 2-3 official romance titles (cute/romantic moments only, never sexual/explicit)."
    prompt = f"""Today is {datetime.now(IST):%d %B %Y}. Pick {MAX_TITLES} titles for a YouTube Shorts edit channel.
Prioritize titles that are currently trending this week (new episodes, just-released films, viral official clips),
plus 2-3 official romance/love titles (romantic or cute moments only; never sexual or explicit).{gta_note}
We need OFFICIAL best-SCENE clips only (never trailers, teasers, TV spots, or fan edits).
Do not pick: {json.dumps(sorted(used)[:80])[:2000]}
Known official studio/distributor names: {json.dumps(official)[:2000]}
Return JSON {{"requests": [...]}} each item:
{{"slug": "lowercaseid", "words": "DISPLAY TITLE",
 "search_queries": ["<title> official fight scene clip", "<title> official clip best scene"],
 "official_channels": ["Netflix", "TOHO animation"],
 "x_handles": ["NetflixAnime"],
 "x_urls": ["https://x.com/OfficialAccount/status/... only if you are sure it is an official studio scene post, not a trailer"],
 "hook": "3-7 word English character-specific curiosity hook"}}
Only list official studio/distributor channels (Netflix, TOHO, Aniplex, Warner, Marvel, Sony,
Prime Video, Disney, etc.). Crunchyroll is official but often has a burned-in logo — you may
list it, we will skip logo'd uploads and look for another official clip of the same title.
NEVER suggest unofficial or fan channels."""
    try:
        data = gemini.generate_json(prompt, purpose="discover-links", temperature=0.5, timeout=90)
    except Exception as e:
        log(f"gemini error {type(e).__name__}")
        return []
    reqs = (data or {}).get("requests") if isinstance(data, dict) else None
    if not reqs:
        return []
    out, seen = [], set()
    for r in reqs:
        if not isinstance(r, dict):
            continue
        words = str(r.get("words") or r.get("title") or "").strip()
        slug = norm(r.get("slug") or words)[:24]
        if not words or not slug or slug in seen or slug in used or words.lower() in used:
            continue
        if is_trailer_text(words, str(r.get("title") or "")):
            continue
        seen.add(slug)
        queries = [q for q in (r.get("search_queries") or []) if q and not TRAILER.search(q)]
        if not queries:
            queries = [f"{words} official scene clip", f"{words} official fight scene"]
        x_urls = []
        for u in r.get("x_urls") or []:
            u = submit_link.normalize_url(str(u))
            if submit_link.is_supported_link(u) and re.search(r"x\.com|twitter\.com", u, re.I):
                if not is_trailer_text(u):
                    x_urls.append(u)
        out.append({
            "slug": slug,
            "words": words,
            "hook": r.get("hook") or "",
            "title": r.get("title") or words,
            "official_channels": [c for c in (r.get("official_channels") or []) if c],
            "search_queries": queries[:4],
            "x_handles": [h.lstrip("@") for h in (r.get("x_handles") or []) if h],
            "x_urls": x_urls[:3],
        })
        if len(out) >= MAX_TITLES:
            break
    return out


def _format_seed_topics(root: Path, used: set[str]) -> list[dict]:
    """Love seeds always; GTA 6 official gameplay only after launch. Never before."""
    out = []
    try:
        import edit_format
    except Exception:
        return out
    used_l = {norm(u) for u in used}
    love = edit_format.love_cfg(root)
    for r in love.get("titles") or []:
        if not isinstance(r, dict):
            continue
        words = str(r.get("words") or r.get("title") or r.get("slug") or "").strip()
        slug = norm(r.get("slug") or words)[:24]
        if not words or not slug or slug in used_l or words.lower() in used:
            continue
        out.append({
            "slug": slug,
            "words": words,
            "hook": r.get("hook") or "",
            "title": r.get("title") or words,
            "official_channels": list(r.get("official_channels") or []),
            "search_queries": [q for q in (r.get("search_queries") or []) if q and not TRAILER.search(q)],
            "x_handles": [h.lstrip("@") for h in (r.get("x_handles") or []) if h],
            "x_urls": [],
            "pillar": "romance",
            "edit_format": "love",
        })
    if edit_format.gta6_has_launched():
        g = edit_format.gta6_cfg(root)
        words = str(g.get("title_words") or g.get("words") or "GTA 6")
        slug = norm(g.get("slug") or "gta6")[:24]
        if slug not in used_l and words.lower() not in used:
            qs = [q for q in (g.get("search_queries") or []) if q and not TRAILER.search(q)]
            out.append({
                "slug": slug,
                "words": words,
                "hook": "The map just opened",
                "title": words,
                "official_channels": list(g.get("official_channels") or ["Rockstar Games"]),
                "search_queries": qs or ["GTA 6 official gameplay", "Grand Theft Auto VI official gameplay"],
                "x_handles": [h.lstrip("@") for h in (g.get("x_handles") or []) if h],
                "x_urls": [],
                "pillar": "movie",
                "edit_format": "gta6",
                "content_kind": "gameplay",
            })
    return out


def _drop_unlaunched_gta(topics: list[dict], root: Path | None = None) -> list[dict]:
    try:
        import edit_format
        if edit_format.gta6_has_launched():
            return topics
    except Exception:
        return topics
    kept = []
    for t in topics:
        blob = " ".join([t.get("slug") or "", t.get("words") or "", t.get("title") or ""])
        if edit_format.is_gta6_item(t, blob):
            log(f"skip {t.get('words') or t.get('slug')}: GTA 6 has not launched yet")
            continue
        kept.append(t)
    return kept


def _boost_trending(topics: list[dict], root: Path) -> list[dict]:
    try:
        import edit_format
        names = edit_format.load_trending_titles(root)
        def key(t):
            blob = " ".join([t.get("words") or "", t.get("title") or "", t.get("slug") or ""])
            love = 3.0 if (t.get("edit_format") == "love" or t.get("pillar") == "romance") else 0.0
            gta = 4.0 if t.get("edit_format") == "gta6" else 0.0
            return -(gta + love + edit_format.trending_score(blob, titles=names))
        return sorted(topics, key=key)
    except Exception:
        return topics


def topics_for_run(root: Path) -> list[dict]:
    used = recent_titles(root)
    found = gemini_topics(root, used)
    if found:
        log(f"gemini proposed {len(found)} title(s)")
    else:
        log("gemini unavailable; using clip_requests/trends seeds")
        found = fallback_topics(root, used)
    # Merge official love seeds (and GTA 6 after launch) without dropping Gemini picks.
    seen = {norm(t.get("slug") or t.get("words") or "") for t in found}
    for seed in _format_seed_topics(root, used):
        k = norm(seed.get("slug") or seed.get("words") or "")
        if k and k not in seen:
            found.append(seed)
            seen.add(k)
    found = _drop_unlaunched_gta(found, root)
    found = _boost_trending(found, root)
    return found[: max(MAX_TITLES, 8)]


def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def yt_search(query: str, n: int = 6) -> list[dict]:
    r = _run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings", f"ytsearch{n}:{query}"], timeout=90)
    if r.returncode != 0:
        return []
    try:
        entries = json.loads(r.stdout or "{}").get("entries") or []
    except Exception:
        return []
    out = []
    for e in entries:
        vid = e.get("id") or ""
        if not vid:
            continue
        url = e.get("webpage_url") or e.get("url") or ""
        if not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        out.append({
            "id": f"yt:{vid}",
            "url": url,
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "handle": (e.get("channel_id") or e.get("uploader_id") or ""),
            "duration": e.get("duration") or 0,
            "width": e.get("width") or 0,
            "height": e.get("height") or 0,
            "formats": e.get("formats") or [],
            "platform": "youtube",
        })
    return out


def x_profile_entries(handle: str, n: int = 8) -> list[dict]:
    h = (handle or "").lstrip("@").strip()
    if not h:
        return []
    r = _run(["yt-dlp", "--flat-playlist", "-I", f"1:{n}", "-J", "--no-warnings",
              f"https://x.com/{h}"], timeout=90)
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout or "{}")
        entries = data.get("entries") or []
    except Exception:
        return []
    out = []
    for e in entries:
        vid = str(e.get("id") or e.get("display_id") or "")
        url = e.get("webpage_url") or e.get("url") or ""
        if not vid and not url:
            continue
        if not str(url).startswith("http"):
            url = f"https://x.com/{h}/status/{vid}"
        out.append({
            "id": f"x:{vid or url}",
            "url": url,
            "title": e.get("title") or e.get("description") or "",
            "channel": e.get("channel") or e.get("uploader") or h,
            "handle": h,
            "duration": e.get("duration") or 0,
            "platform": "x",
        })
    return out


def x_search_entries(handle: str, title: str, n: int = 6) -> list[dict]:
    """Title-scoped search of one official X account (yt-dlp, no cookies)."""
    h = (handle or "").lstrip("@").strip()
    if not h or not (title or "").strip():
        return []
    rows = sourcing._flat_x_entries(sourcing.x_search_url(h, title), n=n)
    for e in rows:
        e["handle"] = e.get("handle") or h
        e["platform"] = "x"
    return rows


def probe_entry(url: str) -> dict | None:
    r = _run(["yt-dlp", "-J", "--no-download", "--no-warnings", "--no-playlist", url], timeout=90)
    if r.returncode != 0:
        err = (r.stderr or "") + (r.stdout or "")
        if BOT.search(err):
            return {"bot_check": True, "url": url}
        return None
    try:
        e = json.loads(r.stdout or "{}")
    except Exception:
        return None
    vid = str(e.get("id") or "")
    host = (urlparse(url).hostname or "").lower()
    plat = "x" if "x.com" in host or "twitter.com" in host else "youtube"
    return {
        "id": f"{plat[:2]}:{vid}" if vid else url,
        "url": e.get("webpage_url") or url,
        "title": e.get("title") or e.get("description") or "",
        "channel": e.get("channel") or e.get("uploader") or "",
        "handle": (e.get("uploader_id") or e.get("channel_id") or ""),
        "duration": e.get("duration") or 0,
        "platform": plat,
    }


def filter_official_candidates(
    entries: list[dict],
    *,
    official: set[str],
    deny: set[str],
    seen: set[str],
) -> list[dict]:
    """Keep official scene clips only. Unofficial never pass, even if clean."""
    out, got = [], set()
    for e in entries:
        url = submit_link.normalize_url(e.get("url") or "")
        cid = e.get("id") or url
        if not url or cid in got or cid in seen or url in seen:
            continue
        if is_denied(cid, url, deny):
            continue
        title = e.get("title") or ""
        if is_trailer_text(url, title) or JUNK.search(title) or sourcing._BAD_TITLE.search(title):
            continue
        try:
            import edit_format
            if edit_format.is_explicit_text(title, e.get("description") or "", e.get("channel") or ""):
                continue
        except Exception:
            pass
        if not submit_link.is_supported_link(url):
            continue
        dur = e.get("duration") or 0
        try:
            dur = float(dur)
        except Exception:
            dur = 0
        if dur and not (20 <= dur <= 480):
            continue
        if not sourcing.formats_meet_1080(e.get("formats"), e.get("width") or 0, e.get("height") or 0):
            continue
        ch = e.get("channel") or e.get("uploader") or ""
        handle = e.get("handle") or ""
        if not is_known_official(ch, handle, official):
            continue
        got.add(cid)
        item = dict(e)
        item["url"] = url
        item["stamped"] = is_stamped_channel(ch) or is_stamped_channel(handle)
        out.append(item)
    # Prefer clean official studios (Netflix/Toho first); stamped official last.
    out.sort(key=lambda c: (1 if c.get("stamped") else 0, sourcing.channel_preference(c.get("channel") or "")))
    return out


def collect_candidates(req: dict, official: set[str], deny: set[str],
                       *, yt=yt_search, x_profile=x_profile_entries,
                       x_search=x_search_entries, probe=probe_entry) -> list[dict]:
    words = req.get("words") or req.get("slug") or ""
    queries = [q for q in (req.get("search_queries") or []) if q and not TRAILER.search(q)]
    if not queries:
        queries = [f"{words} official scene clip", f"{words} official fight scene"]
    extra = {norm(c) for c in (req.get("official_channels") or []) if norm(c) and len(norm(c)) >= 3}
    names = set(official) | extra
    # Extra names must still look like known studios — do not let Gemini invent a fan channel.
    names = {n for n in names if n in official or is_known_official(n, official=official)}
    found: list[dict] = []
    seen: set[str] = set()
    # Link mode: official X (title search on official accounts) first, then YouTube.
    handles = list(sourcing.official_x_handles_for(req))
    for h in list(req.get("x_handles") or []) + list(OFFICIAL_X_HANDLES):
        h = (h or "").lstrip("@")
        if h and norm(h) not in {norm(x) for x in handles}:
            if is_known_official(h, h, names):
                handles.append(h)
    film = words
    x_raw: list[dict] = []
    for h in handles[:8]:
        rows = []
        if film:
            try:
                rows = list(x_search(h, film) or [])
            except TypeError:
                rows = list(x_search(h, film, 6) or []) if False else []
            except Exception:
                rows = []
        if not rows:
            try:
                rows = list(x_profile(h) or [])
            except TypeError:
                rows = []
            except Exception:
                rows = []
            if film:
                rows = [e for e in rows if sourcing.x_post_matches_title(e.get("title") or "", film)]
        for e in rows:
            e = dict(e)
            e["platform"] = "x"
            e["handle"] = e.get("handle") or h
            x_raw.append(e)
            found.append(e)
    for u in req.get("x_urls") or []:
        meta = probe(u)
        if not meta or meta.get("bot_check"):
            if meta and meta.get("bot_check"):
                found.append({"id": u, "url": u, "title": "", "channel": "", "handle": "",
                              "duration": 0, "platform": "x", "bot_check": True})
            continue
        meta["platform"] = "x"
        x_raw.append(meta)
        found.append(meta)
    for q in queries[:3]:
        try:
            found.extend(yt(q, 6))
        except TypeError:
            found.extend(yt(q))
    kept = filter_official_candidates(found, official=names, deny=deny, seen=seen)
    kept.sort(key=lambda c: (
        0 if (c.get("platform") == "x" or sourcing.is_x_url(c.get("url") or "")) else 1,
        1 if c.get("stamped") else 0,
        sourcing.channel_preference(c.get("channel") or ""),
    ))
    collect_candidates.last_x_stats = sourcing.x_resolution_stats(x_raw)
    return kept


collect_candidates.last_x_stats = {"x_total": 0, "x_pass_1080": 0, "x_below_1080": 0, "x_unknown": 0}


class SampleResult:
    __slots__ = ("ok", "reason", "bot_check")

    def __init__(self, ok: bool, reason: str = "", bot_check: bool = False):
        self.ok = ok
        self.reason = reason
        self.bot_check = bot_check


def sample_logo_check(url: str, work: Path) -> SampleResult:
    """Download a short mid-clip sample and run overlay_guard. Never crop/blur."""
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "sample.mp4"
    for old in work.glob("sample*"):
        old.unlink(missing_ok=True)
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--no-cookies",
        "--download-sections", "*0:04-0:16",
        "--force-keyframes-at-cuts",
        "-f", "bv*[height<=720][height>=240]+ba/b[height<=720]/bv*",
        "--merge-output-format", "mp4",
        "-o", str(dest),
        url,
    ]
    try:
        r = _run(cmd, timeout=240)
    except Exception as e:
        return SampleResult(False, f"sample error {type(e).__name__}")
    err = (r.stderr or "") + (r.stdout or "")
    if BOT.search(err) or (r.returncode != 0 and BOT.search(err)):
        return SampleResult(False, "youtube_bot_check", bot_check=True)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 20_000:
        # One fallback: a slightly longer sample window, still not a full render.
        cmd[cmd.index("*0:04-0:16")] = "*0:02-0:20"
        try:
            r = _run(cmd, timeout=240)
        except Exception as e:
            return SampleResult(False, f"sample error {type(e).__name__}")
        err = (r.stderr or "") + (r.stdout or "")
        if BOT.search(err):
            return SampleResult(False, "youtube_bot_check", bot_check=True)
        if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 20_000:
            return SampleResult(False, "sample download failed")
    try:
        why = overlay_guard.clip_reason(dest)
    except Exception as e:
        return SampleResult(False, f"logo check error {type(e).__name__}")
    finally:
        for old in work.glob("sample*"):
            old.unlink(missing_ok=True)
    if why:
        return SampleResult(False, why)
    return SampleResult(True)


def pick_clean_official(
    cands: list[dict],
    *,
    check,
    already=None,
    official: set[str] | None = None,
    max_samples: int = MAX_SAMPLE_PER_TITLE,
) -> tuple[dict | None, list[dict]]:
    """Return the first official clip that passes the logo check.

    Logo'd official clips are skipped; unofficial clips are never considered.
    """
    official = official if official is not None else official_names()
    skipped = []
    samples = 0
    for c in cands:
        url = c.get("url") or ""
        ch = c.get("channel") or c.get("uploader") or ""
        handle = c.get("handle") or ""
        if not is_known_official(ch, handle, official):
            skipped.append({**c, "skip": "unofficial channel ignored"})
            continue
        if already and already(url):
            skipped.append({**c, "skip": "already queued"})
            continue
        if c.get("bot_check"):
            skipped.append({**c, "skip": "youtube_bot_check"})
            continue
        if c.get("stamped") or is_stamped_channel(ch) or is_stamped_channel(handle):
            skipped.append({**c, "skip": "known logo-burn official channel; looking for another official clip"})
            continue
        if not sourcing.formats_meet_1080(c.get("formats"), c.get("width") or 0, c.get("height") or 0):
            skipped.append({**c, "skip": "below 1080p; looking for another official clip"})
            continue
        if samples >= max_samples:
            skipped.append({**c, "skip": "sample budget"})
            continue
        samples += 1
        result = check(url)
        if result.bot_check:
            skipped.append({**c, "skip": "youtube_bot_check"})
            continue
        if not result.ok:
            skipped.append({**c, "skip": result.reason or "logo/watermark"})
            continue
        return c, skipped
    return None, skipped


def queue_accepted(root: Path, cand: dict, req: dict, *, min_ahead: float = 3.0) -> dict:
    hook = (req.get("hook") or "").strip()
    words = req.get("words") or req.get("slug") or ""
    name = req.get("slug") or submit_link.default_slug(cand["url"], cand.get("title") or "", "")
    iso = submit_link.next_free_slot(root, min_ahead_hours=min_ahead)
    item = submit_link.build_item(
        cand["url"],
        title=req.get("title") or words,
        hook=hook or submit_link.default_hook(words),
        name=name,
        slot_iso=iso,
        words=words,
    )
    item["discovered"] = True
    item["discover_channel"] = cand.get("channel") or ""
    if req.get("new_release") or cand.get("new_release"):
        item["new_release"] = True
    path = submit_link.append_queue_item(item, root)
    submit_link.record_request(
        cand["url"], item, status="queued",
        detail=f"auto-discovered official {cand.get('channel') or ''}".strip(),
        root=root,
    )
    log(f"queued {item['id']} slot={item['slot']} from {cand.get('channel')} {cand['url']} -> {path}")
    return item


def write_status(root: Path, payload: dict) -> None:
    path = root / STATUS
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["last_run_at"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def load_status(root: Path) -> dict:
    p = root / STATUS
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def is_new_release_clip(title: str, url: str = "") -> bool:
    """True for a fresh official episode/movie clip; trailers never pass."""
    if is_trailer_text(url, title) or TRAILER.search(title or ""):
        return False
    return bool(NEW_RELEASE.search(title or ""))


def entry_hours_ago(entry: dict, now: datetime) -> float | None:
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    try:
        if ts:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST)
            return (now - dt).total_seconds() / 3600.0
    except Exception:
        pass
    ud = str(entry.get("upload_date") or entry.get("release_date") or "")
    if len(ud) == 8 and ud.isdigit():
        try:
            dt = datetime.strptime(ud, "%Y%m%d").replace(tzinfo=IST)
            return (now - dt).total_seconds() / 3600.0
        except Exception:
            return None
    return None


def list_channel_uploads(url: str, n: int = 8) -> list[dict]:
    """Recent uploads on an official YouTube channel (flat metadata, no download)."""
    r = _run(["yt-dlp", "--flat-playlist", "-I", f"1:{n}", "-J", "--no-warnings",
              "--no-cookies", url], timeout=90)
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout or "{}")
        rows = data.get("entries") or []
    except Exception:
        return []
    out = []
    for e in rows:
        if not isinstance(e, dict):
            continue
        vid = e.get("id") or ""
        href = e.get("webpage_url") or e.get("url") or ""
        if not str(href).startswith("http") and vid:
            href = f"https://www.youtube.com/watch?v={vid}"
        if not href:
            continue
        out.append({
            "id": f"yt:{vid}" if vid else href,
            "url": href,
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "handle": e.get("channel_id") or e.get("uploader_id") or "",
            "duration": e.get("duration") or 0,
            "timestamp": e.get("timestamp") or e.get("release_timestamp") or 0,
            "upload_date": e.get("upload_date") or "",
            "width": e.get("width") or 0,
            "height": e.get("height") or 0,
            "formats": e.get("formats") or [],
            "platform": "youtube",
        })
    return out


def collect_new_releases(
    root: Path,
    now: datetime | None = None,
    since_hours: float = 24.0,
    *,
    list_channel=list_channel_uploads,
    x_profile=x_profile_entries,
) -> list[dict]:
    """Official channel posts from the last 24h that look like a new episode/movie clip."""
    now = now or datetime.now(IST)
    official = official_names(root)
    found: list[dict] = []
    for name, url in OFFICIAL_YT_CHANNELS.items():
        try:
            rows = list(list_channel(url) or [])
        except Exception:
            rows = []
        for e in rows:
            e = dict(e)
            e["channel"] = e.get("channel") or name
            e["platform"] = e.get("platform") or "youtube"
            found.append(e)
    for handle in list(OFFICIAL_X_HANDLES)[:12]:
        try:
            rows = list(x_profile(handle) or [])
        except TypeError:
            rows = []
        except Exception:
            rows = []
        for e in rows:
            e = dict(e)
            e["platform"] = "x"
            e["handle"] = e.get("handle") or handle
            e["channel"] = e.get("channel") or OFFICIAL_X_HANDLES.get(handle, handle)
            found.append(e)
    kept = []
    seen = set()
    for e in found:
        url = submit_link.normalize_url(e.get("url") or "")
        title = e.get("title") or ""
        if not url or url in seen:
            continue
        if is_trailer_text(url, title) or not is_new_release_clip(title, url):
            continue
        age = entry_hours_ago(e, now)
        if age is None or age < 0 or age > since_hours:
            continue
        if not is_known_official(e.get("channel") or "", e.get("handle") or "", official):
            continue
        if already_have_url(root, url):
            continue
        seen.add(url)
        item = dict(e)
        item["url"] = url
        item["age_hours"] = round(age, 2)
        item["new_release"] = True
        kept.append(item)
    kept.sort(key=lambda c: (0 if c.get("platform") == "x" or sourcing.is_x_url(c.get("url") or "") else 1,
                             c.get("age_hours") or 99))
    return kept


def run_discovery(
    root: Path,
    *,
    max_accept: int = DEFAULT_MAX_ACCEPT,
    dry_run: bool = False,
    retry_samples: bool = False,
    collect=None,
    check=None,
    topics=None,
) -> dict:
    official = official_names(root)
    deny = denied_ids(root)
    work = Path(os.environ.get("DISCOVER_WORK") or "/tmp/sky_discover")
    checker = check or (lambda url: sample_logo_check(url, work))
    collector = collect or (lambda req: collect_candidates(req, official, deny))
    accepted, skipped, needs_box, queued = [], [], [], []
    x_stats = {"x_total": 0, "x_pass_1080": 0, "x_below_1080": 0, "x_unknown": 0}
    reqs = list(topics) if topics is not None else (
        (load_status(root).get("needs_box_sample_topics") or []) if retry_samples else topics_for_run(root)
    )
    if retry_samples and not reqs:
        # Re-check official URLs recorded when GitHub hit a YouTube bot check.
        pending = load_status(root).get("needs_box_sample") or []
        by_title: dict[str, dict] = {}
        for row in pending:
            slug = norm(row.get("slug") or row.get("words") or "clip")
            rec = by_title.setdefault(slug, {
                "slug": slug,
                "words": row.get("words") or slug,
                "hook": row.get("hook") or "",
                "title": row.get("title") or row.get("words") or slug,
                "official_channels": [],
                "search_queries": [],
                "x_handles": [],
                "x_urls": [],
                "_cands": [],
            })
            rec["_cands"].append(row)
        reqs = list(by_title.values())

    # Trending-first: official new episode/movie clips from the last 24 hours.
    if not retry_samples and topics is None:
        try:
            fresh = collect_new_releases(root)
        except Exception as e:
            log(f"new-release scan failed: {type(e).__name__}: {e}")
            fresh = []
        log(f"new releases in last 24h: {len(fresh)} official clip(s)")
        checker_nr = checker
        for cand in fresh:
            if len(accepted) >= max_accept:
                break
            pick, why = pick_clean_official(
                [cand], check=checker_nr, already=lambda u: already_have_url(root, u),
            )
            for row in why:
                skipped.append({"slug": "new-release", "url": row.get("url"), "channel": row.get("channel"),
                                "title": row.get("title"), "skip": row.get("skip")})
                if row.get("skip") == "youtube_bot_check":
                    needs_box.append({**row, "official": True, "words": row.get("title") or ""})
            if not pick:
                continue
            req = {
                "slug": submit_link.default_slug(pick["url"], pick.get("title") or "", ""),
                "words": pick.get("title") or "",
                "hook": submit_link.default_hook(pick.get("title") or ""),
                "title": pick.get("title") or "",
            }
            accepted.append({
                "slug": req["slug"], "url": pick["url"], "channel": pick.get("channel"),
                "title": pick.get("title"), "words": req["words"], "hook": req["hook"],
                "new_release": True,
            })
            if dry_run:
                log(f"new-release: would queue {pick['url']} ({pick.get('channel')})")
                continue
            try:
                item = queue_accepted(root, pick, req, min_ahead=1.0)
                item["new_release"] = True
                queued.append(item["id"])
            except SystemExit as e:
                log(f"new-release: could not queue: {e}")

    for req in reqs:
        if len(accepted) >= max_accept:
            break
        cands = req.get("_cands") or collector(req)
        stats = getattr(collect_candidates, "last_x_stats", {}) or {}
        for k in x_stats:
            x_stats[k] += int(stats.get(k) or 0)
        if req.get("_cands"):
            extra = sourcing.x_resolution_stats(req.get("_cands"))
            for k in x_stats:
                x_stats[k] += int(extra.get(k) or 0)
        log(f"{req.get('slug')}: {len(cands)} official scene candidate(s)")
        pick, why = pick_clean_official(
            cands,
            check=checker,
            already=lambda u: already_have_url(root, u),
        )
        for row in why:
            rec = {"slug": req.get("slug"), "url": row.get("url"), "channel": row.get("channel"),
                   "title": row.get("title"), "skip": row.get("skip"),
                   "words": req.get("words"), "hook": req.get("hook")}
            skipped.append(rec)
            if row.get("skip") == "youtube_bot_check":
                needs_box.append(rec | {"official": True})
        if not pick:
            if any(r.get("skip") == "youtube_bot_check" for r in why) and not any(
                r.get("skip") not in ("youtube_bot_check", "already queued", "sample budget") for r in why
            ):
                log(f"{req.get('slug')}: official clips found but this runner cannot sample YouTube")
            else:
                log(f"{req.get('slug')}: no clean official clip (unofficial sources ignored)")
            continue
        accepted.append({
            "slug": req.get("slug"),
            "url": pick["url"],
            "channel": pick.get("channel"),
            "title": pick.get("title") or req.get("title"),
            "words": req.get("words"),
            "hook": req.get("hook"),
        })
        if dry_run:
            log(f"{req.get('slug')}: would queue {pick['url']} ({pick.get('channel')})")
            continue
        try:
            item = queue_accepted(root, pick, req)
            queued.append(item["id"])
        except SystemExit as e:
            log(f"{req.get('slug')}: could not queue: {e}")

    if needs_box:
        log("YouTube blocked this runner for some official clips. On the box "
            "(sample only, no render): python3 scripts/discover_links.py --retry-samples")
    payload = {
        "accepted": accepted,
        "queued_ids": queued,
        "skipped": skipped[-80:],
        "needs_box_sample": needs_box,
        "dry_run": dry_run,
        "x_resolution": x_stats,
    }
    if not dry_run:
        write_status(root, payload)
    log(f"done: accepted {len(accepted)}, queued {len(queued)}, skipped {len(skipped)}, needs_box {len(needs_box)}")
    log("X 1080p floor: {x_pass_1080} pass / {x_below_1080} below / {x_unknown} unknown of {x_total}".format(**x_stats))
    if queued:
        print("ids=" + ",".join(queued))
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover official scene clips and queue them")
    ap.add_argument("--max-accept", type=int, default=DEFAULT_MAX_ACCEPT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry-samples", action="store_true",
                    help="Re-sample official URLs that GitHub could not download (box only)")
    ap.add_argument("--root", default="")
    args = ap.parse_args(argv)
    root = Path(args.root or os.environ.get("SKY_ROOT") or REPO)
    run_discovery(
        root,
        max_accept=max(1, min(args.max_accept, 4)),
        dry_run=args.dry_run,
        retry_samples=args.retry_samples,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
