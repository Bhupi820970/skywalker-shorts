#!/usr/bin/env python3
"""Re-balance already-queued days for variety (BiBi 25 Nov) without touching anything in flight.

  python3 scripts/rebalance_queue.py [--until 2026-11-29] [--dry-run]

Movable = status "queued", no results/<id>.json yet (never rendered, not rendering), slot >= now + 3h,
not a demo. Everything else stays exactly where it is and is used as context.
Per movable slot (time order) the best topic is chosen from the movable items themselves plus catalog
topics that still have UNPOSTED release clips:
  HARD  never the same franchise twice on one IST day
  soft  franchise not within ~3 days, neighbour slots a different category, prefer fresh footage,
        keep the current item when it is already fine (stable).
A moved item keeps its hook/title/sources; only id (<date>-<time>-<slug>) and slot change.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts")]
import variety  # noqa: E402

IST = variety.IST
LEAD_H = 3.0
P_NO_FRESH = 12.0     # topic has no unposted clips left (would title-swap at render time)
P_CATALOG = 2.0       # small preference for keeping already-queued items


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def movable(root: Path, it: dict, now: datetime) -> bool:
    if not isinstance(it, dict) or it.get("demo") or it.get("status") != "queued":
        return False
    iid = str(it.get("id") or "")
    if not iid or (root / "results" / f"{iid}.json").exists():
        return False
    dt = variety.slot_of(it)
    return bool(dt and dt >= now + timedelta(hours=LEAD_H))


def fresh_by_slug(root: Path, assets: set[str] | None) -> dict[str, int]:
    if assets is None:
        return {}
    import footage_refill
    return footage_refill.inventory(footage_refill.fresh_assets(assets, root))


def rebalance(root: Path, days: list[str], now: datetime, assets: set[str] | None,
              make_item=None) -> dict:
    root = Path(root)
    cat = variety.load_catalog(root)
    fresh = fresh_by_slug(root, assets)
    day_items = {d: _load(root / "queue" / f"{d}.json", []) or [] for d in days}
    slots, fixed_ids = [], set()
    pool: list[dict] = []          # movable queued items (each usable once)
    for d in days:
        for it in day_items[d]:
            if movable(root, it, now):
                slots.append((variety.slot_of(it), d, it))
                pool.append(it)
            else:
                fixed_ids.add(str(it.get("id") or ""))
    slots.sort(key=lambda x: x[0])
    moving_ids = {str(it.get("id")) for _, _, it in slots}
    # context = every live slot except the movable ones (they are being re-decided)
    ctx = []
    seen = set()
    for d in days:
        base = datetime.fromisoformat(d + "T12:00:00+05:30")
        for c in variety.context(root, base, days=4, exclude_ids=moving_ids, catalog=cat):
            if c["id"] not in seen:
                seen.add(c["id"])
                ctx.append(c)
    # catalog candidates with unposted footage, capacity = fresh clips // 2 (min 1)
    cap: dict[str, int] = {}
    cat_topics = []
    try:
        import backup_stock
        blocked = backup_stock.blocked_slugs(root)
    except Exception:
        blocked = set()
    try:
        import edit_format
        explicit = edit_format.is_explicit_item
    except Exception:
        explicit = lambda t: False  # noqa: E731
    on_release = {a.split("_scene")[0] for a in (assets or set()) if "_scene" in a}
    for slug, t in cat.items():
        n = fresh.get(slug, 0)
        if slug in blocked or explicit(t) or (n <= 0 and slug not in on_release):
            continue
        # no unposted clip left -> one slot at most (render will title-swap it, variety-aware)
        cap[slug] = max(1, n // 2)
        cat_topics.append(t)
    used_q = [variety.slug_of(it) for it in pool]
    for d in days:  # fixed upcoming items (retries, rendering) also hold their footage
        for it in day_items[d]:
            dt = variety.slot_of(it) if isinstance(it, dict) else None
            if str(it.get("id") or "") in fixed_ids and dt and dt >= now - timedelta(hours=1) \
                    and it.get("status") not in ("replaced", "retired"):
                used_q.append(variety.slug_of(it))
    for s in used_q:  # queued items already reserve their footage
        if s in cap:
            cap[s] -= 1
    changes, out_by_day = [], {d: [it for it in day_items[d] if str(it.get("id") or "") in fixed_ids] for d in days}
    pool_left = list(pool)
    for dt, d, orig in slots:
        best = None
        cands = [("q", it) for it in pool_left] + [("c", t) for t in cat_topics if cap.get(variety.slug_of(t), 0) > 0]
        for kind, t in cands:
            slug = variety.slug_of(t)
            p = variety.topic_penalty(t if kind == "c" else {**t, "slug": slug}, dt, ctx, cat)
            if p == variety.FORBIDDEN:
                continue
            if fresh.get(slug, 0) <= 0:
                p += P_NO_FRESH
            if kind == "c":
                p += P_CATALOG
            if t is orig:
                p -= 0.5
            if best is None or p < best[0]:
                best = (p, kind, t)
        if best is None:  # nothing legal: keep the original (logged)
            best = (variety.FORBIDDEN, "q", orig) if orig in pool_left else (variety.FORBIDDEN, "keep", orig)
        _, kind, t = best
        hhmm = dt.astimezone(IST).strftime("%H:%M")
        code = dt.astimezone(IST).strftime("%Y%m%d-%H%M")
        if kind == "c":
            item = (make_item or _default_make)(d, hhmm, t, assets)
            cap[variety.slug_of(t)] = cap.get(variety.slug_of(t), 1) - 1
        else:
            item = dict(t)
            if t in pool_left:
                pool_left.remove(t)
            slug = variety.slug_of({"id": t.get("id")})
            item["id"] = f"{code}-{slug}"
            item["slot"] = dt.astimezone(IST).isoformat()
        out_by_day[d].append(item)
        ctx.append({"id": item["id"], "dt": dt, "franchise": variety.franchise(item if kind != "c" else t),
                    "category": variety.category(item if kind != "c" else t, cat)})
        if item["id"] != orig.get("id"):
            changes.append({"slot": f"{d} {hhmm}", "from": orig.get("id"), "to": item["id"]})
    for d in days:
        out_by_day[d].sort(key=lambda it: it.get("slot") or "")
    return {"days": out_by_day, "changes": changes, "dropped": [it.get("id") for it in pool_left]}


def _default_make(day, hhmm, topic, assets):
    import fill_queue
    return fill_queue._make_item(day, hhmm, topic, assets)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(IST)
    end = datetime.fromisoformat(a.until).date() if a.until else (now + timedelta(days=4)).date()
    days, d = [], now.date()
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    import sourcing
    res = rebalance(REPO, days, now, sourcing.release_source_assets())
    for c in res["changes"]:
        print(f"{c['slot']}: {c['from']} -> {c['to']}")
    if res["dropped"]:
        print("dropped (unused, never rendered):", ", ".join(res["dropped"]))
    if not a.dry_run:
        for day, items in res["days"].items():
            p = REPO / "queue" / f"{day}.json"
            if p.exists() or items:
                p.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    cat = variety.load_catalog(REPO)
    for day, items in res["days"].items():
        for it in items:
            print(day, (it.get("slot") or "")[11:16], variety.category(it, cat), variety.franchise(it), it.get("id"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
