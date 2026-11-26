#!/usr/bin/env python3
"""Automatic footage refill: keep ~7 days of slots + 10 backup topics of FRESH official scene clips.

Why this exists (25 Nov): posted_history (67edf2d) correctly blocks every scene clip that was
already posted, but nothing measured how much unposted footage was left. clip_requests.py asked
Gemini for 14 *new* titles a day regardless of need, clip_fetch treated a title with 4+ clips on
the release as "done" even when every clip was already posted, and nobody asked for more scenes
of the titles that actually win (Dandadan love, Arcane, Cyberpunk, JJK, Apothecary Diaries...).

  python3 scripts/footage_refill.py plan [--force] [--max-titles 12]   # inventory + requests
  python3 scripts/footage_refill.py status                               # inventory only (JSON)

`plan` writes config/refill_requests.json (consumed by clip_fetch.py on GitHub for X/non-YouTube
links and by the box service for YouTube/X) and config/refill_status.json. Gemini is optional:
on 429 / timeout / no key the deterministic winner seeds are used, so it never blocks anything.
New clips are named <slug>_scene<N>_h264.mp4 with N past every existing asset, so they are new
scene ids; source video ids already fetched are skipped by clip_fetch done_ids (no repeats).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]

IST = timezone(timedelta(hours=5, minutes=30))
REQ_OUT = "config/refill_requests.json"
STATUS_OUT = "config/refill_status.json"
DAYS = int(os.environ.get("REFILL_DAYS", "7"))
BACKUP_TOPICS = int(os.environ.get("REFILL_BACKUP_TOPICS", "10"))
# Clip pool rule (BiBi 26 Nov): refill as soon as fresh usable clips drop to REFILL_MIN_CLIPS (50) or
# below, and ask for enough titles to get back to ~REFILL_TARGET_CLIPS (100).
MIN_CLIPS = int(os.environ.get("REFILL_MIN_CLIPS", "50"))
TARGET_CLIPS = int(os.environ.get("REFILL_TARGET_CLIPS", "100"))
CLIPS_PER_VIDEO = 2          # render merges same-topic clips; QA wants >=18 s unique shots
MAX_TITLES = 12
_SCENE = re.compile(r"^(?P<slug>[a-z0-9]+)_scene(?P<n>\d+)_h264\.mp4$")

# No sexual content, no piracy wording, no trailers.
NSFW = re.compile(r"(nsfw|hentai|ecchi|fan ?service|lewd|sexy|nude|naked|bikini|lingerie|onlyfans|"
                  r"strip(per|ping)?\b|18\+|uncensored|boob|thirst|seduc|erotic|harem)", re.I)
PIRACY = re.compile(r"(full movie|full episode|free download|torrent|leak(ed)?|camrip|hdcam|"
                    r"\bdubbed full\b|watch online|episode \d+ full)", re.I)
TRAILER = re.compile(r"\b(trailer|teaser|promo|tv spot)\b", re.I)

# Official channels a refill request may name (anything else from Gemini is dropped, so a fan
# channel can never become "official"). Logo-burn channels (Crunchyroll/Muse/Ani-One) are
# filtered later by clip_fetch.official_set anyway.
EXTRA_OFFICIAL = ["Netflix Anime", "Netflix", "TOHO animation", "TOHO", "Aniplex USA", "GKIDS Films",
                  "League of Legends", "Sony Pictures Animation", "Warner Bros. Japan Anime",
                  "Adult Swim", "Movieclips", "Prime Video", "Disney+", "Marvel Entertainment",
                  "Rockstar Games", "PlayStation", "CD PROJEKT RED", "Cyberpunk 2077", "ELDEN RING",
                  "BANDAI NAMCO Entertainment America", "Xbox", "Santa Monica Studio", "Insomniac Games",
                  "Walt Disney Animation Studios", "DreamWorks Animation", "Illumination", "Netflix Animation",
                  "Stranger Things", "Warner Bros. Pictures", "Paramount Pictures", "Universal Pictures"]

# What wins on @Skywalker-k3i (growth review 22-25 Nov): love/romance anime, Arcane, Cyberpunk,
# JJK, Apothecary Diaries. Used as Gemini context and as the no-Gemini fallback.
WINNER_SEEDS = [
    {"slug": "dandadan", "words": "DAN DA DAN", "characters": ["Okarun", "Momo"], "kind": "love",
     "official_channels": ["Netflix Anime"], "hook": "Why Okarun cannot look away from her",
     "search_queries": ["DAN DA DAN clip netflix anime", "DAN DA DAN Okarun Momo scene english dub",
                        "DAN DA DAN Turbo Granny clip", "DAN DA DAN season 2 clip netflix anime"]},
    {"slug": "arcane", "words": "ARCANE", "characters": ["Jinx", "Vi", "Caitlyn"], "kind": "emotional",
     "official_channels": ["Netflix", "League of Legends"], "hook": "Jinx finally loses her mind",
     "search_queries": ["Arcane Jinx scene clip netflix", "Arcane Vi Caitlyn scene clip",
                        "Arcane season 2 clip netflix", "Arcane Jinx Silco scene official"]},
    {"slug": "cyberpunkedgerunners", "words": "CYBERPUNK EDGERUNNERS", "characters": ["David", "Lucy"],
     "kind": "love", "official_channels": ["Netflix Anime", "Netflix"], "hook": "David wanted to be a legend",
     "search_queries": ["Cyberpunk Edgerunners clip netflix anime", "Cyberpunk Edgerunners David Lucy scene",
                        "Cyberpunk Edgerunners english dub clip"]},
    {"slug": "jjk", "words": "JUJUTSU KAISEN", "characters": ["Gojo", "Yuji", "Sukuna"], "kind": "fight",
     "official_channels": ["TOHO animation", "TOHO"], "hook": "Why everyone feared Gojo",
     "search_queries": ["Jujutsu Kaisen 0 clip english", "Jujutsu Kaisen official clip TOHO animation",
                        "Jujutsu Kaisen Gojo scene official clip"]},
    {"slug": "apothecarydiaries", "words": "APOTHECARY DIARIES", "characters": ["Maomao", "Jinshi"],
     "kind": "love", "official_channels": ["TOHO animation", "Netflix Anime"],
     "hook": "Why Jinshi cannot stop watching her",
     "search_queries": ["The Apothecary Diaries clip english dub", "Apothecary Diaries Maomao Jinshi scene",
                        "Apothecary Diaries official clip"]},
    {"slug": "yourname", "words": "YOUR NAME", "characters": ["Taki", "Mitsuha"], "kind": "love",
     "official_channels": ["GKIDS Films", "TOHO"], "hook": "Why Taki kept forgetting her name",
     "search_queries": ["Your Name official clip english", "Your Name Taki Mitsuha scene clip GKIDS"]},
    {"slug": "suzume", "words": "SUZUME", "characters": ["Suzume", "Souta"], "kind": "love",
     "official_channels": ["TOHO", "GKIDS Films"], "hook": "Why Suzume would not let him go",
     "search_queries": ["Suzume official clip english", "Suzume Souta scene clip"]},
    {"slug": "violetevergarden", "words": "VIOLET EVERGARDEN", "characters": ["Violet", "Gilbert"],
     "kind": "love", "official_channels": ["Netflix Anime", "Netflix"], "hook": "Why Violet never stopped writing",
     "search_queries": ["Violet Evergarden clip netflix", "Violet Evergarden movie scene english"]},
    {"slug": "spyxfamily", "words": "SPY X FAMILY", "characters": ["Loid", "Yor", "Anya"], "kind": "love",
     "official_channels": ["TOHO animation"], "hook": "Why Loid cannot read Yor",
     "search_queries": ["Spy x Family Code White clip english", "Spy x Family Loid Yor scene official clip"]},
    {"slug": "frieren", "words": "FRIEREN", "characters": ["Frieren", "Fern", "Stark"], "kind": "emotional",
     "official_channels": ["TOHO animation"], "hook": "Why Frieren finally cried",
     "search_queries": ["Frieren official clip english dub", "Frieren Himmel scene clip"]},
    {"slug": "demonslayer", "words": "DEMON SLAYER", "characters": ["Tanjiro", "Nezuko", "Rengoku"],
     "kind": "fight", "official_channels": ["Aniplex USA"], "hook": "Why Rengoku refused to fall",
     "search_queries": ["Demon Slayer official clip english dub Aniplex", "Demon Slayer Infinity Castle clip"]},
    {"slug": "weatheringwithyou", "words": "WEATHERING WITH YOU", "characters": ["Hodaka", "Hina"],
     "kind": "love", "official_channels": ["GKIDS Films", "TOHO"], "hook": "Why Hodaka chose her over the sky",
     "search_queries": ["Weathering With You official clip english", "Weathering With You Hodaka Hina scene"]},
    # --- video games (official gameplay / cinematics from the publisher; trailers stay blocked)
    {"slug": "cyberpunk2077", "category": "game", "words": "CYBERPUNK 2077", "characters": ["V", "Johnny Silverhand"],
     "kind": "action", "official_channels": ["Cyberpunk 2077", "CD PROJEKT RED"], "hook": "Why Johnny never left V's head",
     "search_queries": ["Cyberpunk 2077 official gameplay", "Cyberpunk 2077 Phantom Liberty official cinematic",
                        "Cyberpunk 2077 Johnny Silverhand official cinematic"]},
    {"slug": "eldenring", "category": "game", "words": "ELDEN RING", "characters": ["Tarnished", "Malenia"],
     "kind": "action", "official_channels": ["ELDEN RING", "BANDAI NAMCO Entertainment America"],
     "hook": "Why nobody beats Malenia first try",
     "search_queries": ["ELDEN RING official gameplay", "ELDEN RING Shadow of the Erdtree official gameplay reveal"]},
    {"slug": "gta5", "category": "game", "words": "GTA V", "characters": ["Trevor", "Michael", "Franklin"],
     "kind": "action", "official_channels": ["Rockstar Games"], "hook": "Why Trevor scares even Michael",
     "search_queries": ["Grand Theft Auto V official gameplay video Rockstar Games",
                        "Grand Theft Auto Online official gameplay Rockstar Games"]},
    {"slug": "godofwar", "category": "game", "words": "GOD OF WAR", "characters": ["Kratos", "Atreus"],
     "kind": "emotional", "official_channels": ["PlayStation", "Santa Monica Studio"], "hook": "Why Kratos finally said son",
     "search_queries": ["God of War Ragnarok official gameplay PlayStation", "God of War Ragnarok story cinematic PlayStation"]},
    {"slug": "spiderman2game", "category": "game", "words": "MARVEL'S SPIDER-MAN 2", "characters": ["Peter", "Miles", "Venom"],
     "kind": "action", "official_channels": ["PlayStation", "Insomniac Games"], "hook": "Why Peter could not stop Venom",
     "search_queries": ["Marvel's Spider-Man 2 official gameplay PlayStation", "Marvel's Spider-Man 2 gameplay reveal"]},
    # --- cartoons / western animation
    {"slug": "spiderverse", "category": "cartoon", "words": "SPIDER-VERSE", "characters": ["Miles", "Gwen", "Miguel"],
     "kind": "action", "official_channels": ["Sony Pictures Entertainment", "Sony Pictures Animation"],
     "hook": "Why Miles refused to follow the canon",
     "search_queries": ["Across the Spider-Verse official clip", "Spider-Man Into the Spider-Verse official clip Sony Pictures"]},
    {"slug": "kungfupanda", "category": "cartoon", "words": "KUNG FU PANDA", "characters": ["Po", "Tai Lung"],
     "kind": "action", "official_channels": ["DreamWorks Animation", "Universal Pictures"], "hook": "Why Tai Lung feared Po",
     "search_queries": ["Kung Fu Panda official clip DreamWorks", "Kung Fu Panda Po vs Tai Lung clip"]},
    {"slug": "zootopia", "category": "cartoon", "words": "ZOOTOPIA", "characters": ["Judy", "Nick"],
     "kind": "comedy", "official_channels": ["Walt Disney Animation Studios", "Disney+"], "hook": "Why Nick never trusted anyone",
     "search_queries": ["Zootopia official clip Disney", "Zootopia 2 official clip"]},
    # --- movies
    {"slug": "dune", "category": "movie", "words": "DUNE", "characters": ["Paul Atreides", "Chani"],
     "kind": "action", "official_channels": ["Warner Bros. Pictures", "Movieclips"], "hook": "Why Paul stopped being human",
     "search_queries": ["Dune Part Two official clip", "Dune Part Two Paul sandworm ride clip"]},
    {"slug": "interstellar", "category": "movie", "words": "INTERSTELLAR", "characters": ["Cooper", "Murph"],
     "kind": "emotional", "official_channels": ["Paramount Pictures", "Movieclips"], "hook": "Why Cooper could not look away",
     "search_queries": ["Interstellar official clip", "Interstellar Cooper messages scene clip"]},
    {"slug": "topgunmaverick", "category": "movie", "words": "TOP GUN MAVERICK", "characters": ["Maverick", "Rooster"],
     "kind": "action", "official_channels": ["Paramount Pictures"], "hook": "Why Maverick broke every rule",
     "search_queries": ["Top Gun Maverick official clip", "Top Gun Maverick dogfight clip Paramount"]},
    # --- web series / TV
    {"slug": "strangerthings", "category": "tv", "words": "STRANGER THINGS", "characters": ["Eleven", "Max", "Vecna"],
     "kind": "emotional", "official_channels": ["Netflix", "Stranger Things"], "hook": "Why Vecna wanted Max",
     "search_queries": ["Stranger Things official clip Netflix", "Stranger Things Max Running Up That Hill scene clip"]},
    {"slug": "wednesday", "category": "tv", "words": "WEDNESDAY", "characters": ["Wednesday", "Enid"],
     "kind": "comedy", "official_channels": ["Netflix"], "hook": "Why Wednesday never smiles",
     "search_queries": ["Wednesday official clip Netflix", "Wednesday dance scene clip Netflix"]},
    {"slug": "squidgame", "category": "tv", "words": "SQUID GAME", "characters": ["Gi-hun", "Front Man"],
     "kind": "emotional", "official_channels": ["Netflix"], "hook": "Why Gi-hun went back in",
     "search_queries": ["Squid Game official clip Netflix", "Squid Game season 2 clip Netflix"]},
]
for _s in WINNER_SEEDS:
    _s.setdefault("category", "cartoon" if _s["slug"] == "arcane" else "anime")


def now_ist() -> datetime:
    return datetime.now(IST)


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())[:24]


def slots_per_day() -> int:
    try:
        import slot_times
        return len(slot_times.official_times()) or 8
    except Exception:
        return 8


def threshold(days: int = DAYS, backup_topics: int = BACKUP_TOPICS) -> int:
    """Videos of fresh footage we always want on hand."""
    return days * slots_per_day() + backup_topics


def fresh_assets(assets: set[str] | None, root: Path) -> set[str]:
    """Release scene clips that are usable (not quarantined) and never posted / not held by stock."""
    if assets is None:
        return set()
    sc = {a for a in assets if _SCENE.match(a)}
    try:
        import title_swap
        sc = title_swap.usable_release_assets(sc, root) or set()
        sc = title_swap.unposted_release_assets(sc, root, reserved=True) or set()
    except Exception:
        pass
    return sc


def inventory(fresh: set[str]) -> dict[str, int]:
    out: Counter = Counter()
    for a in fresh:
        m = _SCENE.match(a)
        if m:
            out[m.group("slug")] += 1
    return dict(out)


def supply_videos(inv: dict[str, int]) -> int:
    return sum(inv.values()) // CLIPS_PER_VIDEO


def _item_slug(it: dict) -> str:
    parts = str(it.get("id") or "").split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return "-".join(parts[2:]).lower()
    return slugify(it.get("slug") or "")


def upcoming_queue(root: Path, days: int = DAYS, now: datetime | None = None) -> list[dict]:
    now = now or now_ist()
    out = []
    for i in range(days):
        day = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        for it in _load(root / "queue" / f"{day}.json", []) or []:
            if isinstance(it, dict):
                out.append(it)
    return out


def winners(root: Path, n: int = 12) -> list[tuple[str, int]]:
    """Topic slugs ranked by channel views (stats/latest.json queue_id -> slug)."""
    vids = (_load(root / "stats" / "latest.json", {}) or {}).get("videos") or {}
    score: Counter = Counter()
    rows = vids.values() if isinstance(vids, dict) else vids
    for v in rows:
        if not isinstance(v, dict):
            continue
        slug = _item_slug({"id": v.get("queue_id") or ""})
        slug = re.sub(r"^stock-", "", slug)
        slug = re.sub(r"-\d+$", "", slug)
        if slug:
            score[slugify(slug)] += int(v.get("views") or 0)
    return score.most_common(n)


def allowed_channels(root: Path) -> list[str]:
    names = set(EXTRA_OFFICIAL)
    for lst in ((_load(root / "config" / "source_catalog.json", {}) or {}).get("official_channels") or {}).values():
        names |= set(lst)
    try:
        import sourcing
        names |= set(sourcing.OFFICIAL_X_HANDLES.values())
    except Exception:
        pass
    return sorted(names)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_request(r: dict, allowed: list[str], catalog: dict[str, dict]) -> dict | None:
    """Validate one request (Gemini or seed). Returns None when it breaks a content rule."""
    if not isinstance(r, dict) or not (r.get("words") or r.get("slug")):
        return None
    text = " ".join(str(r.get(k) or "") for k in ("words", "hook", "title", "slug")) + " " + \
        " ".join(str(q) for q in r.get("search_queries") or [])
    if NSFW.search(text) or PIRACY.search(text):
        return None
    slug = slugify(r.get("slug") or r.get("words"))
    if not slug:
        return None
    ok = {_norm(c): c for c in allowed}
    chans = [ok[_norm(c)] for c in r.get("official_channels") or [] if _norm(c) in ok]
    if not chans:
        chans = ["Netflix Anime", "Netflix", "TOHO animation"]
    qs = [str(q).strip() for q in r.get("search_queries") or []
          if str(q).strip() and not TRAILER.search(str(q)) and not NSFW.search(str(q)) and not PIRACY.search(str(q))]
    words = str(r.get("words") or slug.upper()).upper()[:60]
    if not qs:
        qs = [f"{words.title()} official clip", f"{words.title()} scene official clip english"]
    links = [u for u in r.get("links") or [] if isinstance(u, str)
             and re.match(r"^https://(x|twitter)\.com/[A-Za-z0-9_]+/status/\d+", u)]
    t = catalog.get(slug) or {}
    hook = str(r.get("hook") or t.get("hook") or f"Why {words.title()} hits different")[:80]
    out = {"slug": slug, "pillar": r.get("pillar") or t.get("pillar") or "anime", "words": words,
           "characters": [str(c) for c in (r.get("characters") or [])][:5],
           "scene_types": r.get("scene_types") or (["romance", "emotional"] if r.get("kind") == "love"
                                                   else ["fight", "emotional", "aura"]),
           "search_queries": qs[:6], "official_channels": chans[:5], "links": links[:4], "hook": hook,
           "title": str(r.get("title") or t.get("title") or f"{hook} | {words.title()} | #edit")[:100],
           "tags": [slugify(x) for x in (r.get("tags") or t.get("tags") or [slug, "anime", "edit", "shorts"])][:8],
           "market": r.get("market") or t.get("market") or "US/UK", "refill": True,
           "category": _cat(r, t, slug),
           "prefer_english": True, "known_topic": slug in catalog}
    return out


def _cat(r: dict, t: dict, slug: str) -> str:
    import variety
    c = str(r.get("category") or "").lower()
    return c if c in variety.CATEGORIES else variety.category({**t, **r, "slug": slug})


def balance(reqs: list[dict], n: int) -> list[dict]:
    """Round-robin over categories (anime, movie, cartoon, game, tv) so one kind never floods the queue."""
    import variety
    by: dict[str, list[dict]] = defaultdict(list)
    for r in reqs:
        by[r.get("category") or "movie"].append(r)
    order = ["anime", "movie", "cartoon", "game", "tv"]
    out: list[dict] = []
    while len(out) < n and any(by[c] for c in order):
        for c in order:
            if by[c] and len(out) < n:
                out.append(by[c].pop(0))
    return out


def gemini_requests(root: Path, wins: list, inv: dict, deficit: int, n: int) -> list[dict]:
    """One Gemini call (own budget file, short timeout). [] on 429 / timeout / no key."""
    os.environ.setdefault("SKY_GEMINI_BUDGET_FILE", "/tmp/gemini_refill_budget.json")
    os.environ.setdefault("GEMINI_MAX_CALLS", "2")
    os.environ.setdefault("GEMINI_MAX_HTTP", "8")
    os.environ.setdefault("GEMINI_CALL_MAX_HTTP", "6")
    try:
        import gemini
        if not gemini.available():
            return []
        trends = _load(root / "trends" / "latest.json", {}) or {}
        prompt = f"""Today is {now_ist():%d %B %Y}. "Skywalker" is a YouTube Shorts channel of anime/movie scene edits
(dialogue line at the start, then the edit; white hook bar). We are running out of FRESH official scene clips:
every clip already posted is blocked forever, so we need NEW scenes. Short by ~{deficit} videos.
Channel winners (topic slug -> views): {json.dumps(wins)[:1200]}
Unposted clips left per topic: {json.dumps(inv)[:800]}
What wins: love/romance anime (Dandadan Okarun/Momo, Apothecary Diaries Jinshi/Maomao, Your Name), Arcane (Jinx),
Cyberpunk Edgerunners (David/Lucy), JJK, scene-specific curiosity hooks. Trend notes: {json.dumps(trends.get('our_insights') or trends.get('hook_styles') or '')[:800]}

Return JSON {{"requests": [...]}} with {n} items as a BALANCED MIX of categories: anime, movie, cartoon (western
animation like Spider-Verse / Arcane), video game (official publisher gameplay or story cinematics: GTA, Cyberpunk 2077,
Elden Ring, God of War...), and web series / TV. Within each category: more DIFFERENT SCENES of our winners first,
then trending titles with the same vibe. Never the same franchise twice. Rules: OFFICIAL studio/distributor uploads only (Netflix, Netflix Anime, TOHO animation,
Aniplex USA, GKIDS Films, League of Legends, Movieclips, ...), never fan re-uploads, never trailers/teasers, never
full episodes, nothing sexual or fan-service. Prefer clips with clear ENGLISH dialogue (English dub or English-language).
Each item: {{"category": "anime"|"movie"|"cartoon"|"game"|"tv", "slug": lowercase a-z0-9 (reuse the winner slug for more scenes of a winner), "pillar": "anime"|"hollywood"|"bollywood"|"south",
"words": display title in caps, "characters": [...], "scene_types": subset of ["romance","emotional","fight","aura","comedy"],
"search_queries": [3-5 YouTube search strings for official scene clips, include "clip" and the official channel name],
"official_channels": [exact official channel names], "links": [official X post URLs https://x.com/<handle>/status/<id> ONLY if certain, else []],
"hook": 4-8 word English scene-specific curiosity hook, "tags": [6 lowercase tags], "market": "US/UK" or "IN"}}"""
        data = gemini.generate_json(prompt, purpose="footage-refill", temperature=0.6, timeout=75)
        reqs = (data or {}).get("requests") if isinstance(data, dict) else None
        return [r for r in reqs or [] if isinstance(r, dict)]
    except Exception as e:  # never let Gemini break the refill
        print(f"::warning::refill gemini skipped ({type(e).__name__})")
        return []


def seed_requests(wins: list, inv: dict) -> list[dict]:
    """Deterministic fallback: winner seeds, best-performing first, emptiest inventory first."""
    rank = {s: v for s, v in wins}

    def key(s):
        views = max([v for w, v in rank.items() if w.startswith(s["slug"]) or s["slug"].startswith(w)] or [0])
        return (inv.get(s["slug"], 0), -views)
    return sorted(WINNER_SEEDS, key=key)


def build_plan(root: Path, assets: set[str] | None, *, force: bool = False, max_titles: int = MAX_TITLES,
               use_gemini: bool = True, now: datetime | None = None) -> dict:
    now = now or now_ist()
    fresh = fresh_assets(assets, root)
    inv = inventory(fresh)
    have = supply_videos(inv)
    need = threshold()
    queue = upcoming_queue(root, now=now)
    starving = sorted({_item_slug(it) for it in queue if _item_slug(it) and not inv.get(_item_slug(it).replace("-", ""))})
    fresh_n = sum(inv.values())
    clip_deficit = max(0, TARGET_CLIPS - fresh_n) if fresh_n <= MIN_CLIPS else 0
    deficit = max(0, need - have, -(-clip_deficit // CLIPS_PER_VIDEO))
    status = {"at": now.strftime("%Y-%m-%d %H:%M IST"), "fresh_clips": sum(inv.values()),
              "fresh_videos": have, "threshold_videos": need, "deficit_videos": deficit,
              "min_clips": MIN_CLIPS, "target_clips": TARGET_CLIPS, "deficit_clips": clip_deficit,
              "days": DAYS, "backup_topics": BACKUP_TOPICS, "release_known": assets is not None,
              "queue_items_next_days": len(queue), "queue_topics_without_fresh_clips": starving[:40],
              "inventory": dict(sorted(inv.items(), key=lambda kv: -kv[1])), "requests": [], "source": "none"}
    if assets is None:
        status["note"] = "release asset list unavailable; no plan this run"
        return status
    if deficit <= 0 and not force:
        status["note"] = "enough fresh footage; nothing requested"
        return status
    catalog = {t.get("slug"): t for t in (_load(root / "config" / "topic_catalog.json", {}) or {}).get("items") or []}
    allowed = allowed_channels(root)
    wins = winners(root)
    n = max(1, min(max_titles, (deficit // 3) + 4 if deficit else max_titles))
    raw = gemini_requests(root, wins, inv, deficit, n) if use_gemini else []
    status["source"] = "gemini" if raw else "seeds"
    raw = raw + seed_requests(wins, inv)  # seeds always backfill (and cover Gemini 429)
    import variety
    cleaned, seen, seen_fr = [], set(), set()
    for r in raw:
        c = clean_request(r, allowed, catalog)
        if not c or c["slug"] in seen or variety.franchise(c["slug"]) in seen_fr:
            continue
        seen.add(c["slug"])
        seen_fr.add(variety.franchise(c["slug"]))
        cleaned.append(c)
    out = balance(cleaned, n)
    status["categories"] = dict(Counter(r["category"] for r in out))
    status["requests"] = [r["slug"] for r in out]
    status["doc"] = {"generated_at": status["at"], "max_clips_per_title": 4, "min_height": 1080,
                     "deficit_videos": deficit, "requests": out}
    return status


def release_assets() -> set[str] | None:
    try:
        import sourcing
        return sourcing.release_source_assets(refresh=True)
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="plan", choices=["plan", "status"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-titles", type=int, default=MAX_TITLES)
    ap.add_argument("--no-gemini", action="store_true")
    a = ap.parse_args(argv)
    st = build_plan(REPO, release_assets(), force=a.force,
                    max_titles=a.max_titles, use_gemini=not a.no_gemini and a.cmd == "plan")
    doc = st.pop("doc", None)
    if a.cmd == "plan":
        (REPO / STATUS_OUT).write_text(json.dumps(st, indent=1, ensure_ascii=False) + "\n")
        if doc:
            (REPO / REQ_OUT).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: st[k] for k in ("fresh_clips", "fresh_videos", "threshold_videos", "deficit_videos",
                                         "source", "requests")}, ensure_ascii=False))
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"need={'1' if doc else '0'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
