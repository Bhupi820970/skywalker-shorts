#!/usr/bin/env python3
"""Weekly hit report: top titles, hooks, and songs by views.

Writes stats/weekly_hits.json. perf.load_perf merges the weights so title,
hook, and music picks lean toward what worked this week.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

IST = timezone(timedelta(hours=5, minutes=30))
OUT = "stats/weekly_hits.json"
LOOKBACK_DAYS = 7


def _parse_published(s: str):
    if not s:
        return None
    try:
        raw = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except Exception:
        return None


def _slug(queue_id: str, result: dict | None = None) -> str:
    if result:
        tags = [str(t) for t in (result.get("tags") or []) if t not in ("edit", "shorts", "trending", "test")]
        if tags:
            return tags[0].lower()
    parts = (queue_id or "").split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return "-".join(parts[2:])
    return queue_id or ""


def _weight(views: float, likes: float = 0.0) -> float:
    return math.log1p(max(0.0, views)) + 0.35 * math.log1p(max(0.0, likes))


def load_results_by_id(root: Path) -> dict:
    out = {}
    for p in (root / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("id"):
            out[r["id"]] = r
        title = " ".join((r.get("title") or "").split()).casefold()
        if title:
            out.setdefault("title:" + title, r)
    return out


def build_weekly_hits(root: Path | None = None, now: datetime | None = None,
                      lookback_days: int = LOOKBACK_DAYS) -> dict:
    root = root or REPO
    now = now or datetime.now(IST)
    cutoff = now - timedelta(days=lookback_days)
    try:
        stats = json.loads((root / "stats" / "latest.json").read_text())
    except Exception:
        stats = {}
    results = load_results_by_id(root)
    rows = []
    for vid, x in (stats.get("videos") or {}).items():
        pub = _parse_published(x.get("published") or "")
        # Keep videos published this week; if no date, still include (young channel / scrape).
        if pub and pub < cutoff:
            continue
        views = float(x.get("views") or 0)
        if views <= 0:
            continue
        res = {}
        qid = x.get("queue_id") or ""
        if qid and qid in results:
            res = results[qid]
        else:
            res = results.get("title:" + " ".join((x.get("title") or "").split()).casefold()) or {}
        hook = (res.get("hook") or x.get("hook") or "").strip()
        music = (res.get("music") or x.get("music") or "")
        music = str(music).rstrip("/").split("/")[-1]
        slug = _slug(qid, res)
        rows.append({
            "video_id": vid,
            "queue_id": qid,
            "slug": slug,
            "title": x.get("title") or res.get("title") or "",
            "hook": hook,
            "music": music,
            "views": views,
            "likes": float(x.get("likes") or 0),
            "published": x.get("published") or "",
        })
    rows.sort(key=lambda r: -r["views"])

    def top_group(key: str, n: int = 8) -> list[dict]:
        best: dict[str, dict] = {}
        for r in rows:
            k = (r.get(key) or "").strip()
            if not k:
                continue
            prev = best.get(k.casefold())
            if not prev or r["views"] > prev["views"]:
                best[k.casefold()] = {"name": k, "views": r["views"], "likes": r["likes"],
                                      "title": r.get("title") or "", "slug": r.get("slug") or ""}
        return sorted(best.values(), key=lambda x: -x["views"])[:n]

    titles = top_group("slug")
    hooks = top_group("hook")
    songs = top_group("music")
    weights = {
        "titles": {t["name"]: round(_weight(t["views"], t["likes"]), 4) for t in titles if t["name"]},
        "hooks": {h["name"]: round(_weight(h["views"], h["likes"]), 4) for h in hooks if h["name"]},
        "songs": {s["name"]: round(_weight(s["views"], s["likes"]), 4) for s in songs if s["name"]},
    }
    all_views = [r["views"] for r in rows]
    mid = 0.0
    if all_views:
        s = sorted(all_views)
        m = len(s) // 2
        mid = s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2
    report = {
        "week_of": (now - timedelta(days=lookback_days - 1)).date().isoformat(),
        "through": now.date().isoformat(),
        "at": now.strftime("%Y-%m-%d %H:%M IST"),
        "lookback_days": lookback_days,
        "median_views": mid,
        "n": len(rows),
        "top_titles": titles,
        "top_hooks": hooks,
        "top_songs": songs,
        "weights": weights,
    }
    return report


def write_weekly_hits(root: Path | None = None, now: datetime | None = None) -> dict:
    root = root or REPO
    report = build_weekly_hits(root, now)
    path = root / OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"weekly_hits: {report['n']} videos, median {report['median_views']:.0f}, "
          f"top title {((report['top_titles'] or [{}])[0].get('name') or '-')}",
          flush=True)
    return report


def main() -> int:
    write_weekly_hits()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
