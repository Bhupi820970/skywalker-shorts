#!/usr/bin/env python3
"""Ensure each upcoming day has 8 official IST slots filled from topic_catalog.

Keeps existing queue items (including legacy 12:30/17:30). Never retimes or
deletes posts. Used by trends.yml and can be run manually.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import hook_text  # noqa: E402
import perf  # noqa: E402
import slot_times  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
GH = "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources"
QUEUE = REPO / "queue"
CATALOG = REPO / "config" / "topic_catalog.json"


def _day_file(day: str) -> Path:
    return QUEUE / f"{day}.json"


def _load_day(day: str) -> list[dict]:
    p = _day_file(day)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def release_assets() -> set[str] | None:
    """Asset names on the sources release (None when unknown, e.g. offline)."""
    import subprocess
    try:
        r = subprocess.run(["gh", "api", "--paginate", "repos/chatgpt820970/skywalker-shorts/releases/tags/sources",
                            "-q", ".assets[].name"], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return set(r.stdout.split())
    except Exception:
        pass
    try:
        import urllib.request
        url = "https://api.github.com/repos/chatgpt820970/skywalker-shorts/releases/tags/sources"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "skywalker"}), timeout=30) as r:
            return {a["name"] for a in json.loads(r.read()).get("assets") or []}
    except Exception:
        return None


def topic_assets(topic: dict, have: set[str] | None) -> list[str]:
    """Official scene clips first, trailer only as the last fallback; only assets that exist."""
    names = list(topic.get("scene_assets") or [])
    if topic.get("fallback_asset"):
        names.append(topic["fallback_asset"])
    if have is None:
        return names
    return [n for n in names if n in have]


def _used_slugs() -> set[str]:
    used = set()
    for p in QUEUE.glob("2026-*.json"):
        try:
            items = json.loads(p.read_text())
        except Exception:
            continue
        for it in items:
            if it.get("status") in ("replaced", "retired"):
                continue
            parts = (it.get("id") or "").split("-")
            if len(parts) >= 3 and parts[0].isdigit():
                used.add("-".join(parts[2:]).lower())
            for t in it.get("tags") or []:
                used.add(str(t).lower())
    return used


def _make_item(day: str, hhmm: str, topic: dict, have: set[str] | None = None) -> dict:
    code = day.replace("-", "") + "-" + hhmm.replace(":", "")
    slug = topic["slug"]
    assets = topic_assets(topic, have)
    asset = assets[0] if assets else None
    item = {
        "id": f"{code}-{slug}",
        "slot": slot_times.slot_iso(day, hhmm),
        "pillar": topic.get("pillar") or "movie",
        "source_url": topic.get("source_url") or "",
        "template": "whitebar",
        "hook": topic["hook"],
        "words": topic.get("words") or slug.upper(),
        "title": topic["title"],
        "description": topic["description"],
        "tags": list(topic.get("tags") or [])[:10],
        "target_dur": max(24, min(29, int(topic.get("target_dur") or 26))),
        "status": "queued",
        "market": topic.get("market") or "US/UK",
        "content_kind": topic.get("content_kind") or "trailer",
    }
    if asset and "_scene" in asset and item["content_kind"] == "trailer":
        item["content_kind"] = "best scene"
    if asset:
        url = f"{GH}/{asset}"
        item["fallback_mp4"] = [f"{GH}/{a}" for a in assets]
        item["sources"] = [url]
        item["fallback_release"] = "sources"
        item["fallback_asset"] = asset
    if topic.get("alt_source_urls"):
        item["alt_source_urls"] = list(topic["alt_source_urls"])
    return item


def fill_day(day: str, catalog: list[dict], used: set[str], have: set[str] | None = None) -> int:
    items = _load_day(day)
    existing_isos = [it.get("slot") for it in items if it.get("slot")]
    need = slot_times.next_official_to_add(existing_isos, day)
    if not need:
        return 0
    added = 0
    # rotate catalog: prefer unused slugs, weighted by stats performance
    if have is not None:
        # never queue a topic whose official clip/trailer is not on the sources release yet
        catalog = [t for t in catalog if topic_assets(t, have)] or catalog
    pool = [t for t in catalog if t["slug"] not in used]
    if not pool:
        pool = list(catalog)
    try:
        perf_data = perf.load_perf(REPO)
    except Exception:
        perf_data = {}
    rng = random.Random(day)

    def topic_rank(t: dict) -> float:
        base = 0.0
        if perf_data:
            try:
                base = float(perf.topic_bonus(perf_data, t.get("tags"), t.get("pillar")))
            except Exception:
                base = 0.0
        # Real scene clips beat trailer-only topics (trailer edits look low quality).
        if t.get("scene_assets"):
            base += 2.0
        # Exploration jitter so we do not lock onto one pillar forever.
        return base + rng.random() * 4.0

    pool = sorted(pool, key=topic_rank, reverse=True)
    pi = 0
    for iso in need:
        hhmm = iso[11:16]
        topic = pool[pi % len(pool)]
        pi += 1
        # avoid same slug twice same day, and skip banned hook/title templates
        day_slugs = {"-".join(it["id"].split("-")[2:]) for it in items if it.get("id")}
        item = None
        tries = 0
        while tries < len(pool):
            cand = _make_item(day, hhmm, topic, have)
            bad = topic["slug"] in day_slugs or hook_text.check_item_hook_fields(cand)
            if not bad:
                item = cand
                break
            topic = pool[pi % len(pool)]
            pi += 1
            tries += 1
        if item is None:
            continue
        items.append(item)
        used.add(topic["slug"])
        added += 1
    items.sort(key=lambda it: it.get("slot") or "")
    _day_file(day).write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    return added


def days_ahead(n: int | None = None) -> list[str]:
    n = n if n is not None else max(slot_times.MIN_AHEAD_DAYS + 1, 3)
    now = datetime.now(IST).date()
    return [(now + timedelta(days=i)).isoformat() for i in range(0, n + 1)]


def main() -> int:
    cat = json.loads(CATALOG.read_text()).get("items") or []
    if not cat:
        print("empty topic catalog", file=sys.stderr)
        return 1
    used = _used_slugs()
    have = release_assets()
    total = 0
    # Always cover through min_ahead_days (plus one extra day of buffer).
    targets = days_ahead()
    for day in targets:
        if day < "2026-11-14":
            continue
        n = fill_day(day, cat, used, have)
        if n:
            print(f"{day}: added {n} slot(s)")
        total += n
    print(f"fill_queue done added={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
