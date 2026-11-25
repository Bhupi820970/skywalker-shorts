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
sys.path.insert(0, str(REPO / "scripts"))
import hook_text  # noqa: E402
import sourcing  # noqa: E402
import perf  # noqa: E402
import slot_times  # noqa: E402
import backup_stock  # noqa: E402
import variety  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
GH = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources"
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
        r = subprocess.run(["gh", "api", "--paginate", "repos/Bhupi820970/skywalker-shorts/releases/tags/sources",
                            "-q", ".assets[].name"], capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return set(r.stdout.split())
    except Exception:
        pass
    try:
        import urllib.request
        url = "https://api.github.com/repos/Bhupi820970/skywalker-shorts/releases/tags/sources"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "skywalker"}), timeout=30) as r:
            return {a["name"] for a in json.loads(r.read()).get("assets") or []}
    except Exception:
        return None


def topic_assets(topic: dict, have: set[str] | None) -> list[str]:
    """Official scene clips only (never trailers/teasers); only assets that exist."""
    names: list[str] = []
    for n in list(topic.get("scene_assets") or []):
        if n and not sourcing.is_trailer_source(n) and not sourcing.is_unusable_source(n, topic) and n not in names:
            names.append(n)
    fa = topic.get("fallback_asset") or ""
    if fa and not sourcing.is_trailer_source(fa) and not sourcing.is_unusable_source(fa, topic) and "_scene" in fa.lower() and fa not in names:
        names.append(fa)
    if have is None:
        return names
    return [n for n in names if n in have]


def _item_slug(it: dict) -> str:
    parts = (it.get("id") or "").split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return "-".join(parts[2:]).lower()
    return ""


def _norm_text(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _used_in_days(days: list[str]) -> tuple[set[str], set[str]]:
    """Slugs and hooks already queued in the given days.

    Only the item id slug is counted (not tags), so a tag like
    "dandadan" cannot mark a different topic as already used.
    """
    slugs: set[str] = set()
    hooks: set[str] = set()
    for day in days:
        for it in _load_day(day):
            if it.get("status") in ("replaced", "retired"):
                continue
            s = _item_slug(it)
            if s:
                slugs.add(s)
            hook = _norm_text(it.get("hook") or "")
            if hook:
                hooks.add(hook)
            title = _norm_text((it.get("title") or "").split("#")[0])
            if title:
                hooks.add(title)
    return slugs, hooks


def _used_slugs() -> set[str]:
    """Backward-compatible helper: slugs queued in the fill window."""
    slugs, _hooks = _used_in_days(_window_days())
    return slugs


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
        "content_kind": topic.get("content_kind") or "best scene",
    }
    try:
        import edit_format
        fmt = edit_format.infer_format(
            topic, pillar=item["pillar"],
            text=" ".join([item.get("hook") or "", item.get("words") or "", item.get("title") or ""]),
        )
        if fmt == "love":
            item["pillar"] = "romance"
            item["edit_format"] = "love"
            item["content_kind"] = topic.get("content_kind") or "romance scene"
            item["target_dur"] = max(24, min(29, int(item.get("target_dur") or 26)))
        elif fmt == "gta6":
            item["edit_format"] = "gta6"
            item["content_kind"] = "gameplay"
    except Exception:
        pass
    if asset:
        item["fallback_mp4"] = [f"{GH}/{a}" for a in assets]
        item["sources"] = [f"{GH}/{a}" for a in assets]
        item["fallback_release"] = "sources"
        item["fallback_asset"] = asset
    if topic.get("alt_source_urls"):
        item["alt_source_urls"] = list(topic["alt_source_urls"])
    return item


def fill_day(day: str, catalog: list[dict], used: set[str], have: set[str] | None = None, used_hooks: set[str] | None = None) -> int:
    items = _load_day(day)
    existing_isos = [it.get("slot") for it in items if it.get("slot")]
    need = slot_times.next_official_to_add(existing_isos, day)
    if not need:
        return 0
    added = 0
    # rotate catalog: prefer unused slugs, weighted by stats performance
    # never queue a topic that has no official scene clips (trailers are not used)
    catalog = [t for t in catalog if topic_assets(t, have)]
    try:
        blocked = backup_stock.blocked_slugs(REPO)
    except Exception:
        blocked = set()
    if blocked:
        catalog = [t for t in catalog if (t.get("slug") or "").lower() not in blocked]
    if not catalog:
        return 0
    used_hooks = used_hooks if used_hooks is not None else set()
    pool = [t for t in catalog if t["slug"] not in used]
    # Do not fall back to already-queued slugs; identical hooks in the
    # lookahead window weaken the channel. Leave a slot empty rather than
    # clone a title already scheduled this window (backup stock covers it).
    if not pool:
        return 0
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
                base += float(perf.title_bonus(perf_data, t.get("slug")))
            except Exception:
                base = 0.0
        # Prefer titles with 3+ official scene clips so the render can merge shots.
        n_scenes = len(topic_assets(t, have))
        if n_scenes >= 5:
            base += 4.5
        elif n_scenes >= 3:
            base += 3.0
        elif n_scenes:
            base += 1.5
        try:
            import edit_format
            blob = " ".join([t.get("words") or "", t.get("title") or "", t.get("slug") or "",
                             t.get("pillar") or "", " ".join(t.get("tags") or [])])
            base += edit_format.trending_score(blob, titles=edit_format.load_trending_titles(REPO))
        except Exception:
            pass
        # Exploration jitter so we do not lock onto one pillar forever.
        return base + rng.random() * 4.0

    pool = sorted(pool, key=topic_rank, reverse=True)
    try:
        import edit_format
        quotas = edit_format.daily_quotas()
    except Exception:
        quotas = {"love_min": 0, "love_max": 0, "gta6_min": 0, "gta6_max": 0, "gta6_launched": False}

    def _kind(t: dict) -> str:
        try:
            import edit_format
            return edit_format.infer_format(t, pillar=str(t.get("pillar") or ""),
                                            text=" ".join([t.get("words") or "", t.get("hook") or "",
                                                           t.get("title") or "", t.get("slug") or ""]))
        except Exception:
            if (t.get("pillar") or "").lower() in ("romance", "love"):
                return "love"
            return "action"

    # Drop GTA 6 topics until official launch.
    if not quotas.get("gta6_launched"):
        pool = [t for t in pool if _kind(t) != "gta6"]
        if not pool:
            return 0

    # Variety (BiBi 25 Nov): never the same franchise twice a day, avoid it for ~3 days, and
    # rotate categories (movie / game / cartoon / anime / tv) so neighbour slots differ.
    vcat = variety.load_catalog(REPO)
    try:
        vctx = variety.context(REPO, datetime.fromisoformat(day + "T12:00:00+05:30"), catalog=vcat)
    except Exception:
        vctx = []
    cur = {"dt": None}

    def vpen(t: dict) -> float:
        return variety.topic_penalty(t, cur["dt"], vctx, vcat)

    def next_topic(love_n, gta_n, start_i, day_slugs):
        # Fill love / GTA quotas first when matching topics remain.
        want = None
        if love_n < quotas.get("love_min", 0):
            want = "love"
        elif gta_n < quotas.get("gta6_min", 0):
            want = "gta6"
        elif love_n < quotas.get("love_max", 0):
            want = "love"
        elif gta_n < quotas.get("gta6_max", 0):
            want = "gta6"
        n = len(pool)
        best = None
        for k in range(n):
            t = pool[(start_i + k) % n]
            if t["slug"] in day_slugs:
                continue
            try:
                import edit_format as _ef
                if _ef.is_explicit_item(t):
                    continue
            except Exception:
                pass
            kind = _kind(t)
            if want and kind != want:
                continue
            if not want and kind == "love" and love_n >= quotas.get("love_max", 0):
                continue
            if not want and kind == "gta6" and gta_n >= quotas.get("gta6_max", 0):
                continue
            p = vpen(t)
            if p == variety.FORBIDDEN:
                continue
            if best is None or p < best[0]:
                best = (p, t, start_i + k + 1)
            if p == 0:
                break
        if best:
            return best[1], best[2]
        # Fallback: any unused topic whose franchise is not already on this day
        for k in range(n):
            t = pool[(start_i + k) % n]
            if t["slug"] not in day_slugs and vpen(t) != variety.FORBIDDEN:
                return t, start_i + k + 1
        return None, start_i + 1

    pi = 0
    love_n = sum(1 for it in items if (it.get("pillar") or "").lower() in ("romance", "love")
                 or it.get("edit_format") == "love")
    gta_n = sum(1 for it in items if "gta6" in (it.get("id") or "").lower()
                or it.get("edit_format") == "gta6")
    for iso in need:
        hhmm = iso[11:16]
        # avoid same slug twice same day, and skip banned hook/title templates
        day_slugs = {"-".join(it["id"].split("-")[2:]) for it in items if it.get("id")}
        item = None
        tries = 0
        cur["dt"] = variety.parse_slot(iso)
        topic, pi = next_topic(love_n, gta_n, pi, day_slugs)
        while topic is not None and tries < len(pool):
            cand = _make_item(day, hhmm, topic, have)
            hook_n = _norm_text(cand.get("hook") or "")
            title_n = _norm_text((cand.get("title") or "").split("#")[0])
            dup = (
                topic["slug"] in day_slugs
                or topic["slug"] in used
                or (hook_n and hook_n in used_hooks)
                or (title_n and title_n in used_hooks)
            )
            bad = dup or hook_text.check_item_hook_fields(cand)
            if not bad:
                item = cand
                break
            topic, pi = next_topic(love_n, gta_n, pi, day_slugs | {topic["slug"]})
            tries += 1
        if item is None:
            continue
        items.append(item)
        vctx.append({"id": item["id"], "dt": cur["dt"], "franchise": variety.franchise(topic),
                     "category": variety.category(topic, vcat)})
        used.add(topic["slug"])
        hook_n = _norm_text(item.get("hook") or "")
        if hook_n:
            used_hooks.add(hook_n)
        title_n = _norm_text((item.get("title") or "").split("#")[0])
        if title_n:
            used_hooks.add(title_n)
        added += 1
        if item.get("edit_format") == "love" or (item.get("pillar") or "").lower() in ("romance", "love"):
            love_n += 1
        if item.get("edit_format") == "gta6":
            gta_n += 1
    items.sort(key=lambda it: it.get("slot") or "")
    _day_file(day).write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    return added


def days_ahead(n: int | None = None) -> list[str]:
    n = n if n is not None else max(slot_times.MIN_AHEAD_DAYS + 1, 3)
    now = datetime.now(IST).date()
    return [(now + timedelta(days=i)).isoformat() for i in range(0, n + 1)]


def _window_days(n: int | None = None) -> list[str]:
    """Yesterday through the fill horizon, so we do not re-queue a title
    that just ran or is already on the upcoming calendar."""
    targets = days_ahead(n)
    if not targets:
        return []
    first = datetime.fromisoformat(targets[0]).date()
    yesterday = (first - timedelta(days=1)).isoformat()
    return [yesterday, *targets]


def main() -> int:
    cat = json.loads(CATALOG.read_text()).get("items") or []
    if not cat:
        print("empty topic catalog", file=sys.stderr)
        return 1
    used, used_hooks = _used_in_days(_window_days())
    have = release_assets()
    total = 0
    # Always cover through min_ahead_days (plus one extra day of buffer).
    targets = days_ahead()
    for day in targets:
        if day < "2026-11-14":
            continue
        n = fill_day(day, cat, used, have, used_hooks)
        if n:
            print(f"{day}: added {n} slot(s)")
        total += n
    print(f"fill_queue done added={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
