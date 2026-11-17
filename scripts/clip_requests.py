#!/usr/bin/env python3
"""Daily: Gemini (key rotation GEMINI_API_KEY.._6) decides which official scene clips we need.

Reads trends/latest.json + our own performance + recently used topics and writes
config/clip_requests.json. The box (scripts/clip_fetch.py, YouTube allowed) and GitHub
(clip_fetch.py --no-youtube: DailyMotion/Bilibili/Archive/Vimeo) both consume it.
"""
from __future__ import annotations
import json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "scripts"))
import gemini  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OUT = REPO / "config" / "clip_requests.json"
PILLARS = ("anime", "hollywood", "bollywood", "south")


def recent_topics(days: int = 7) -> set[str]:
    """Slugs/words queued or posted in the last `days` days (no repeats)."""
    cut = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
    used = set()
    for p in (REPO / "queue").glob("2026-*.json"):
        if p.stem < cut:
            continue
        try:
            items = json.loads(p.read_text())
        except Exception:
            continue
        for it in items:
            parts = (it.get("id") or "").split("-")
            if len(parts) >= 3 and parts[0].isdigit():
                used.add("-".join(parts[2:]).lower())
            if it.get("words"):
                used.add(str(it["words"]).lower())
    return used


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())[:24]


def main() -> int:
    os.environ.setdefault("SKY_GEMINI_BUDGET_FILE", "/tmp/gemini_clipreq_budget.json")
    os.environ.setdefault("GEMINI_MAX_CALLS", "3")
    now = datetime.now(IST)
    trends = {}
    try:
        trends = json.loads((REPO / "trends" / "latest.json").read_text())
    except Exception:
        pass
    try:
        import trends as tr  # scripts/trends.py
        ours = tr.own_performance()[:25]
    except Exception:
        ours = []
    recent = recent_topics()
    cat = json.loads((REPO / "config" / "topic_catalog.json").read_text()).get("items") or []
    channels = json.loads((REPO / "config" / "source_catalog.json").read_text()).get("official_channels") or {}
    prompt = f"""Today is {now:%d %B %Y}. You pick SOURCE CLIPS for "Skywalker", a YouTube Shorts channel of movie/anime edits
(8/day, white hook bar, phonk/slowed songs). Best performers so far: Apothecary Diaries "Why Jinshi cannot stop watching her" (~19k),
Hunger Games Haymitch (~11.6k), Doctor Doom (~6.2k), Gru (~4.3k), SRK Jawan (~3.7k). Character-specific curiosity hooks win;
"aura in this scene" titles flop.
Our recent stats: {json.dumps(ours, ensure_ascii=False)[:3500]}
Trend notes: {json.dumps({k: trends.get(k) for k in ("our_insights", "top_channels", "hook_styles", "avoid")}, ensure_ascii=False)[:3000]}
DO NOT pick these (used in the last 7 days): {json.dumps(sorted(recent))[:2500]}
Known official channels: {json.dumps(channels)[:1500]}

Return JSON {{"requests": [...]}} with 14 items: about 5 anime, 4 hollywood, 3 bollywood, 2 south (Telugu/Tamil/Kannada).
Pick trending or evergreen titles whose OFFICIAL distributor/studio channels post at least 3 real SCENE CLIPS in Full HD or 4K.
NEVER pick trailer-only titles. NEVER ask for trailers, teasers, or TV spots, even as a fallback.
Each item: {{"slug": short lowercase a-z0-9 id, "pillar": one of {list(PILLARS)}, "words": display title in caps,
"characters": [main characters], "scene_types": subset of ["fight","romance","emotional","aura","comedy","entrance"],
"search_queries": [3-5 YouTube search strings for official scene clips only (no trailer/teaser words), e.g. "<title> <character> fight scene official clip"],
"official_channels": [exact official channel names that upload CLEAN clips (no burned-in logo or subtitles), e.g. Netflix, Netflix Anime, TOHO, TOHO animation, studio/distributor channels, Movieclips, YRF, Sony Pictures Entertainment. NEVER list Crunchyroll, Muse, or Ani-One — their YouTube uploads burn in a logo/subtitles],
"hook": 3-7 word English character-specific curiosity hook (like "Why Jinshi cannot stop watching her"),
"title": "<hook> | <scene> | #edit", "tags": [6-8 lowercase tags], "market": "US/UK" or "IN"}}"""
    data = gemini.generate_json(prompt, purpose="clip-requests", temperature=0.7, timeout=120)
    reqs = (data or {}).get("requests") if isinstance(data, dict) else None
    if not reqs:
        print("::warning::clip request generation failed; keeping previous config/clip_requests.json")
        return 0
    known = {t["slug"]: t for t in cat}
    out, seen = [], set()
    for r in reqs:
        if not isinstance(r, dict) or not r.get("words"):
            continue
        slug = slugify(r.get("slug") or r["words"])
        if not slug or slug in seen or slug in recent or str(r["words"]).lower() in recent:
            continue
        seen.add(slug)
        r["slug"] = slug
        r["pillar"] = r.get("pillar") if r.get("pillar") in PILLARS else "hollywood"
        r["known_topic"] = slug in known
        out.append(r)
    doc = {"generated_at": now.strftime("%Y-%m-%d %H:%M IST"), "max_clips_per_title": 5,
           "min_height": 1080, "requests": out}
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"clip_requests: {len(out)} -> " + ", ".join(f"{r['slug']}({r['pillar']})" for r in out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
