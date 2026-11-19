#!/usr/bin/env python3
"""Queue part 2 and part 3 when a Short beats the channel median views.

Different official scene clips and hooks from the parent Short; never repeat
shots or trailers. Videos are never deleted. GitHub-side only (stats.yml).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import hook_text  # noqa: E402
import sourcing  # noqa: E402
import submit_link  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
GH = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources"
MAX_TITLES_PER_RUN = 2
PARTS = (2, 3)


def median_views(views: list[float | int]) -> float:
    nums = sorted(float(v) for v in views if v is not None)
    if not nums:
        return 0.0
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def load_stats(root: Path) -> dict:
    for name in ("latest.json",):
        p = root / "stats" / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    return {}


def _slug_of(iid: str, result: dict | None = None) -> str:
    if result:
        for key in ("stock_topic", "slug", "series_slug"):
            s = str(result.get(key) or "")
            if s.startswith("backup-"):
                s = s[len("backup-"):]
            s = s.replace("-part2", "").replace("-part3", "")
            if s:
                return s
        tags = [str(t) for t in (result.get("tags") or []) if t and t not in ("edit", "shorts", "trending", "test")]
        if tags:
            return tags[0].lower()
    iid = iid or ""
    parts = iid.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        slug = "-".join(parts[2:])
        return slug.replace("-part2", "").replace("-part3", "")
    if iid.startswith("stock-"):
        parsed = slug = iid[len("stock-"):]
        return parsed.rsplit("-", 1)[0] if "-" in parsed else parsed
    return iid


def source_keys(value) -> set[str]:
    out = set()
    if not value:
        return out
    if isinstance(value, (list, tuple)):
        for v in value:
            out |= source_keys(v)
        return out
    s = str(value)
    name = s.rstrip("/").split("/")[-1].split("?")[0].lower()
    if name.endswith(".mp4") or name:
        out.add(name)
    if s.startswith("http"):
        out.add(s.rstrip("/").lower())
    return {x for x in out if x and x not in ("gh:",)}


def used_shots(result: dict | None) -> set[str]:
    if not result:
        return set()
    used = set()
    for key in ("source", "fallback_asset", "used_source", "asset"):
        used |= source_keys(result.get(key))
    for key in ("fallback_mp4", "sources", "scene_assets", "alt_source_urls", "avoid_sources"):
        used |= source_keys(result.get(key))
    return {u for u in used if not sourcing.is_trailer_source(u)}


def catalog_topic(root: Path, slug: str) -> dict | None:
    try:
        items = json.loads((root / "config" / "topic_catalog.json").read_text()).get("items") or []
    except Exception:
        return None
    slug = (slug or "").lower()
    for t in items:
        if (t.get("slug") or "").lower() == slug:
            return t
    return None


def unused_official_clips(topic: dict, used: set[str]) -> list[str]:
    assets = []
    for a in list(topic.get("scene_assets") or []) + list(topic.get("fallback_mp4") or []):
        name = str(a).rstrip("/").split("/")[-1].split("?")[0]
        if not name.endswith(".mp4") or sourcing.is_trailer_source(name):
            continue
        keys = source_keys(name) | source_keys(f"{GH}/{name}")
        if keys & used:
            continue
        if name not in assets:
            assets.append(name)
    return assets


def sequel_hooks(topic: dict, used_hooks: set[str]) -> list[str]:
    catalog = (topic.get("hook") or "").strip()
    words = (topic.get("words") or topic.get("slug") or "this").strip()
    drafts = [
        catalog,
        "This is the part they left out",
        "Nobody talks about this scene",
        "Watch what happens next",
        "The scene after this hits harder",
        f"This {words} scene goes further" if 1 < len(words.split()) <= 4 else "",
    ]
    used = {h.casefold() for h in used_hooks if h}
    out = []
    for h in drafts:
        h = " ".join((h or "").split())
        if not h or h.casefold() in used:
            continue
        probe = {"hook": h, "title": h, "description": h}
        if hook_text.check_item_hook_fields(probe) or hook_text.template_errors(h):
            continue
        out.append(h)
        used.add(h.casefold())
    return out


def existing_parts(root: Path, slug: str) -> set[int]:
    found = {1}
    slug = (slug or "").lower()

    def visit(obj: dict):
        s = _slug_of(obj.get("id") or "", obj)
        if s.lower() != slug and (obj.get("series_slug") or "").lower() != slug:
            return
        part = obj.get("series_part")
        if part:
            try:
                found.add(int(part))
            except Exception:
                pass
        iid = (obj.get("id") or "").lower()
        if "part2" in iid or iid.endswith("p2"):
            found.add(2)
        if "part3" in iid or iid.endswith("p3"):
            found.add(3)

    for p in (root / "results").glob("*.json"):
        try:
            visit(json.loads(p.read_text()))
        except Exception:
            continue
    qdir = root / "queue"
    if qdir.exists():
        for p in qdir.glob("2026-*.json"):
            try:
                for it in json.loads(p.read_text()):
                    visit(it)
            except Exception:
                continue
    return found


def hit_rows(stats: dict, median: float) -> list[dict]:
    rows = []
    for vid, x in (stats.get("videos") or {}).items():
        views = float(x.get("views") or 0)
        if views <= median:
            continue
        iid = x.get("queue_id") or ""
        if not iid:
            continue
        rows.append({"video_id": vid, "views": views, "queue_id": iid,
                     "title": x.get("title") or "", "hook": x.get("hook") or "",
                     "music": x.get("music") or ""})
    rows.sort(key=lambda r: -r["views"])
    return rows


def queue_sequels(root: Path | None = None, now: datetime | None = None,
                  max_titles: int = MAX_TITLES_PER_RUN) -> list[dict]:
    root = root or REPO
    now = now or datetime.now(IST)
    stats = load_stats(root)
    views = [float(x.get("views") or 0) for x in (stats.get("videos") or {}).values()]
    med = median_views(views)
    queued = []
    seen_slugs = set()
    for row in hit_rows(stats, med):
        if len({q["slug"] for q in queued}) >= max_titles:
            break
        parent = {}
        rp = root / "results" / f"{row['queue_id']}.json"
        if rp.exists():
            try:
                parent = json.loads(rp.read_text())
            except Exception:
                parent = {}
        slug = _slug_of(row["queue_id"], parent)
        if not slug or slug in seen_slugs:
            continue
        topic = catalog_topic(root, slug)
        if not topic:
            continue
        used = used_shots(parent)
        used_hooks = {row.get("hook") or "", parent.get("hook") or ""}
        have_parts = existing_parts(root, slug)
        clips = unused_official_clips(topic, used)
        hooks = sequel_hooks(topic, used_hooks)
        if not clips or not hooks:
            continue
        seen_slugs.add(slug)
        for part in PARTS:
            if part in have_parts:
                continue
            if not clips or not hooks:
                break
            asset = clips.pop(0)
            hook = hooks.pop(0)
            try:
                iso = submit_link.next_free_slot(root, now=now, min_ahead_hours=3.0)
            except SystemExit:
                break
            url = f"{GH}/{asset}"
            item = submit_link.build_item(
                url,
                title=submit_link.default_title(hook, topic.get("words") or slug),
                hook=hook,
                name=slug,
                slot_iso=iso,
                words=topic.get("words") or slug.upper(),
                scene_assets=[asset] + clips[:2],
            )
            item["series_part"] = part
            item["series_parent"] = row["queue_id"]
            item["series_slug"] = slug
            item["avoid_sources"] = sorted(used)
            item["pillar"] = topic.get("pillar") or item.get("pillar")
            item["tags"] = list(dict.fromkeys(list(topic.get("tags") or []) + [slug, f"part{part}", "edit"]))[:10]
            submit_link.append_queue_item(item, root)
            used |= source_keys(asset)
            used_hooks.add(hook)
            have_parts.add(part)
            queued.append({"id": item["id"], "slug": slug, "part": part, "slot": iso,
                            "hook": hook, "asset": asset, "parent": row["queue_id"]})
            print(f"series: queued {item['id']} part {part} of {slug} (parent {row['queue_id']} beat median {med:.0f})",
                  flush=True)
    return queued


def main(argv: list[str] | None = None) -> int:
    got = queue_sequels()
    print(json.dumps({"queued": got, "n": len(got)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
