#!/usr/bin/env python3
"""Scene request list for the box downloader (GitHub plans, the box downloads, GitHub edits).

GitHub runners never download from YouTube / X (bot wall). This writes config/scene_requests.json:
for every upcoming slot (queue, soonest first) and then the backup pool, which show and scenes are
wanted, the official search queries, and which `sources` release assets are still missing.
The box (scripts/box_service.py) polls it, downloads official >=1080p scene clips with the box's own
YouTube / X logins and uploads them to the `sources` release; render.yml reads ONLY that release.

  python3 scripts/scene_requests.py            # write config/scene_requests.json
  python3 scripts/scene_requests.py --print    # also print a short summary
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import sourcing  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
OUT = REPO / "config" / "scene_requests.json"
ASSET_RX = re.compile(r"^([a-z0-9]+)_scene(\d+)_h264\.mp4$")
LATE_H = 6.0          # publish skips slots more than 6h late, so those need nothing
HORIZON_H = 48.0      # plan two days ahead
SCENE_TYPES = ["fight", "emotional", "aura"]
LOVE_SCENES = ["romantic", "emotional", "confession"]


def _slot_dt(it: dict) -> datetime | None:
    raw = str(it.get("slot") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def _assets_of(it: dict) -> list[str]:
    out = []
    for u in (it.get("fallback_mp4") or []) + (it.get("sources") or []) + (it.get("scene_assets") or []):
        a = str(u or "").rstrip("/").rsplit("/", 1)[-1]
        if ASSET_RX.match(a) and not sourcing.is_trailer_source(a) and a not in out:
            out.append(a)
    return out


def _slug(it: dict) -> str:
    for a in _assets_of(it):
        return ASSET_RX.match(a).group(1)
    bid = str(it.get("id") or "")
    if bid.startswith("backup-"):
        return bid[len("backup-"):]
    parts = bid.split("-")
    return "-".join(parts[2:]) if len(parts) >= 3 and parts[0].isdigit() else re.sub(r"[^a-z0-9]+", "", bid.lower())


def _queries(words: str, love: bool) -> list[str]:
    kinds = ("romantic scene", "emotional scene", "official clip") if love else (
        "fight scene official clip", "best scene official clip", "official clip")
    return [f"{words} {k}" for k in kinds]


def build(now: datetime | None = None, have: set[str] | None = None) -> dict:
    now = now or datetime.now(IST)
    have = sourcing.release_source_assets() if have is None else have
    cat = sourcing.load_catalog().get("topics") or {}
    reqs: dict[str, dict] = {}

    def add(it: dict, why: str, slot: datetime | None) -> None:
        slug = _slug(it)
        if not slug:
            return
        wanted = _assets_of(it)
        on_release = sourcing.release_scene_assets(slug, have) if have is not None else []
        missing = [a for a in wanted if have is None or a not in have]
        if have is not None and len(on_release) >= 2 and not missing:
            return  # enough clips already
        if have is not None and len(on_release) >= 3:
            return  # render merges what exists (>=3 clips is plenty even if names differ)
        words = str(it.get("words") or slug).strip()
        love = (it.get("edit_format") or "") == "love" or (it.get("pillar") or "") == "romance"
        row = reqs.get(slug)
        key = slot.isoformat() if slot else "9999"
        if row and row["slot"] <= key:
            return
        reqs[slug] = {
            "slug": slug, "slot": key, "for": why, "item_id": it.get("id") or "",
            "pillar": it.get("pillar") or "anime", "words": words,
            "scene_types": LOVE_SCENES if love else SCENE_TYPES,
            "search_queries": _queries(words, love),
            "official_channels": (cat.get(slug) or {}).get("official_channels") or it.get("official_channels") or [],
            "source_url": it.get("source_url") or "",
            "missing_assets": missing, "on_release": on_release,
            "hook": it.get("hook") or "", "title": it.get("title") or "",
            "tags": it.get("tags") or [], "market": it.get("market") or "US/UK",
        }

    start = (now - timedelta(days=1)).date()
    for i in range(4):
        f = REPO / "queue" / f"{start + timedelta(days=i):%Y-%m-%d}.json"
        try:
            items = json.loads(f.read_text())
        except Exception:
            continue
        for it in items:
            if (it.get("status") or "queued") not in ("queued", "retry_queued") or it.get("retired"):
                continue
            dt = _slot_dt(it)
            if dt is None or dt < now - timedelta(hours=LATE_H) or dt > now + timedelta(hours=HORIZON_H):
                continue
            add(it, "slot", dt)
    try:
        pool = json.loads((REPO / "config" / "backup_pool.json").read_text()).get("items") or []
    except Exception:
        pool = []
    for it in pool:
        add(it, "backup_pool", None)
    rows = sorted(reqs.values(), key=lambda r: (r["slot"], r["slug"]))
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M IST"),
        "note": "GitHub never downloads. The box fetches these official >=1080p scene clips (no trailers, "
                "no watermarked / logo-burn channels) and uploads them to the `sources` release.",
        "release_listed": have is not None,
        "release_scene_clips": len([a for a in (have or set()) if ASSET_RX.match(a)]),
        "max_clips_per_title": 3,
        "requests": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args(argv)
    doc = build()
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    slots = sum(1 for r in doc["requests"] if r["for"] == "slot")
    print(f"scene_requests: {len(doc['requests'])} title(s) ({slots} for upcoming slots); "
          f"{doc['release_scene_clips']} scene clip(s) on the release")
    if a.print:
        for r in doc["requests"][:20]:
            print(f"  {r['slot'][:16]} {r['slug']}: missing {len(r['missing_assets'])}, on release {len(r['on_release'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
