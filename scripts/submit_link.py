#!/usr/bin/env python3
"""Turn an official YouTube or X scene-clip link into a queued Skywalker Short.

BiBi (or the box script) passes a URL. This module validates it, rejects
trailers, picks the next free official IST slot, writes a queue item, and
optionally records the request in links/requests.json.

Downloading often fails on GitHub runners (YouTube bot check). In that case
run scripts/fetch_link_to_release.sh on the box, which uploads the file to
the sources release and then triggers render.yml.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import hook_text  # noqa: E402
import slot_times  # noqa: E402
import sourcing  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
GH = "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources"
YT = re.compile(r"(?:youtube\.com|youtu\.be)", re.I)
X = re.compile(r"(?:(?:^|\.)x\.com|(?:^|\.)twitter\.com)", re.I)
MIN_AHEAD_H = 3.0


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("http://"):
        u = "https://" + u[7:]
    return u


def is_supported_link(url: str) -> bool:
    u = normalize_url(url)
    if not u.startswith("https://"):
        return False
    host = (urlparse(u).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"):
        return True
    if host in ("x.com", "twitter.com", "mobile.twitter.com", "vxtwitter.com"):
        return True
    return False


def link_is_trailer(url: str, title: str = "") -> bool:
    if sourcing.is_trailer_source(url) or sourcing.is_trailer_source(title):
        return True
    blob = f"{url} {title}".lower()
    return bool(re.search(r"\b(trailer|teaser|tv\s*spot)\b", blob))


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return s[:32]


def default_slug(url: str, title: str = "", name: str = "") -> str:
    if name and slugify(name):
        return slugify(name)
    if title and slugify(title):
        return slugify(title)
    u = normalize_url(url)
    m = re.search(r"(?:v=|/shorts/|/video/|youtu\.be/)([A-Za-z0-9_-]{6,})", u)
    if m:
        return slugify("clip" + m.group(1)[:12])
    return "cliplink"


def default_hook(title: str) -> str:
    t = " ".join((title or "").split())
    if t and not hook_text.hook_language_errors(t, label="hook"):
        words = t.split()
        if 3 <= len(words) <= 8 and not link_is_trailer("", t):
            return t
    return "Watch what happens in this scene"


def default_title(hook: str, words: str) -> str:
    w = (words or "Official scene").strip()
    h = hook.rstrip(" .")
    return f"{h} | {w} | #edit"


def load_requests(root: Path | None = None) -> dict:
    root = root or REPO
    p = root / "links" / "requests.json"
    if not p.exists():
        return {"note": "Official YouTube or X scene-clip links.", "requests": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"note": "Official YouTube or X scene-clip links.", "requests": []}


def save_requests(doc: dict, root: Path | None = None) -> None:
    root = root or REPO
    p = root / "links" / "requests.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _slot_taken(root: Path, iso: str) -> bool:
    try:
        import backup_stock
        if backup_stock.slot_is_covered(root, iso):
            return True
    except Exception:
        pass
    for p in (root / "queue").glob("*.json"):
        try:
            items = json.loads(p.read_text())
        except Exception:
            continue
        for it in items:
            if it.get("slot") == iso and it.get("status", "queued") not in ("replaced", "retired"):
                # A failed queued item does not block a new link into that slot
                rid = root / "results" / f"{it.get('id')}.json"
                if rid.exists():
                    try:
                        st = json.loads(rid.read_text()).get("status")
                    except Exception:
                        st = None
                    if st in ("failed",):
                        continue
                return True
    for p in (root / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("slot") == iso and r.get("status") == "done" and not r.get("no_publish"):
            return True
    return False


def next_free_slot(root: Path | None = None, now: datetime | None = None,
                   min_ahead_hours: float = MIN_AHEAD_H, slot: str | None = None) -> str:
    """Return an official IST slot ISO string at least min_ahead_hours ahead."""
    root = root or REPO
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    if slot:
        raw = slot.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", raw):
            # today or tomorrow at this official time
            hhmm = f"{int(raw.split(':')[0]):02d}:{raw.split(':')[1]}"
            for add in (0, 1, 2):
                day = (now + timedelta(days=add)).date().isoformat()
                iso = slot_times.slot_iso(day, hhmm)
                dt = datetime.fromisoformat(iso)
                if dt >= now + timedelta(hours=min_ahead_hours) and not _slot_taken(root, iso):
                    return iso
        else:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            iso = dt.isoformat()
            if not _slot_taken(root, iso):
                return iso
            raise SystemExit(f"slot {iso} is already taken")
    earliest = now + timedelta(hours=min_ahead_hours)
    for add in range(0, 5):
        day = (now + timedelta(days=add)).date().isoformat()
        for hhmm in slot_times.official_times():
            iso = slot_times.slot_iso(day, hhmm)
            dt = datetime.fromisoformat(iso)
            if dt < earliest:
                continue
            if not _slot_taken(root, iso):
                return iso
    raise SystemExit("no free official slot in the next 5 days")


def build_item(url: str, *, title: str = "", hook: str = "", name: str = "",
               slot_iso: str = "", words: str = "") -> dict:
    url = normalize_url(url)
    if not is_supported_link(url):
        raise SystemExit("only official YouTube or X (Twitter) links are accepted")
    if link_is_trailer(url, title):
        raise SystemExit("trailers and teasers are not used; give an official scene clip")
    slug = default_slug(url, title, name)
    if not slug:
        raise SystemExit("could not make a slug from the name or title")
    hook = (hook or default_hook(title)).strip()
    errs = hook_text.check_item_hook_fields({"hook": hook, "title": title or hook,
                                             "description": hook})
    if errs and title:
        # title may be a film name; hook must still be clean
        errs = hook_text.hook_language_errors(hook, label="hook")
        if errs:
            raise SystemExit("English-only hook failed: " + "; ".join(errs))
    elif errs:
        raise SystemExit("English-only hook/title failed: " + "; ".join(errs))
    words = (words or title or slug).strip() or slug.upper()
    yt_title = title.strip() if title else default_title(hook, words)
    if hook_text.hook_language_errors(yt_title, label="title"):
        yt_title = default_title(hook, words)
    dt = datetime.fromisoformat(slot_iso)
    code = dt.strftime("%Y%m%d-%H%M")
    asset = f"{slug}_scene1_h264.mp4"
    item = {
        "id": f"{code}-{slug}",
        "slot": slot_iso,
        "pillar": "movie",
        "source_url": url,
        "alt_source_urls": [url],
        "fallback_release": "sources",
        "fallback_asset": asset,
        "fallback_mp4": [f"{GH}/{asset}"],
        "sources": [f"{GH}/{asset}"],
        "template": "whitebar",
        "hook": hook,
        "words": words if words.isupper() or " " in words else words.upper(),
        "title": yt_title,
        "description": f"{hook}. Official scene clip. Edit by Skywalker.",
        "tags": ["edit", "shorts", "trending", slug[:20]],
        "target_dur": 26,
        "status": "queued",
        "market": "US/UK",
        "content_kind": "best scene",
        "link_request": True,
        "demo": False,
        "no_publish": False,
    }
    return item


def append_queue_item(item: dict, root: Path | None = None) -> Path:
    root = root or REPO
    day = item["slot"][:10]
    path = root / "queue" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        items = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        items = []
    items = [it for it in items if it.get("id") != item["id"]]
    items.append(item)
    items.sort(key=lambda it: it.get("slot") or "")
    path.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    return path


def record_request(url: str, item: dict, *, status: str, detail: str = "",
                   root: Path | None = None) -> None:
    root = root or REPO
    doc = load_requests(root)
    rec = {
        "url": url,
        "id": item.get("id"),
        "slug": (item.get("fallback_asset") or "").split("_scene")[0],
        "title": item.get("title"),
        "slot": item.get("slot"),
        "status": status,
        "detail": detail,
        "at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
    }
    reqs = [r for r in (doc.get("requests") or [])
            if not (r.get("url") == url and r.get("status") in ("pending", "new"))]
    reqs.append(rec)
    doc["requests"] = reqs
    save_requests(doc, root)


def pending_requests(root: Path | None = None) -> list[dict]:
    doc = load_requests(root)
    out = []
    for r in doc.get("requests") or []:
        if (r.get("status") or "pending") in ("pending", "new", "") and r.get("url"):
            out.append(r)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Queue an official YouTube/X scene clip as a Short")
    ap.add_argument("--url", default="", help="YouTube or X video URL")
    ap.add_argument("--title", default="", help="Optional display title")
    ap.add_argument("--hook", default="", help="Optional English hook (3-8 words)")
    ap.add_argument("--name", default="", help="Asset slug, e.g. dandadanfight")
    ap.add_argument("--slot", default="", help="Optional IST slot (HH:MM or ISO)")
    ap.add_argument("--words", default="")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--min-ahead", type=float, default=MIN_AHEAD_H)
    ap.add_argument("--pending", action="store_true",
                    help="Process pending rows in links/requests.json")
    args = ap.parse_args(argv)
    root = Path(os.environ.get("SKY_ROOT") or REPO)
    jobs = []
    if args.pending:
        jobs = pending_requests(root)
        if not jobs:
            print("no pending link requests")
            return 0
    elif args.url:
        jobs = [{"url": args.url, "title": args.title, "hook": args.hook,
                 "name": args.name, "slot": args.slot, "words": args.words,
                 "no_publish": args.no_publish}]
    else:
        ap.error("pass --url or --pending")
    ids = []
    for job in jobs:
        url = job.get("url") or args.url
        iso = next_free_slot(root, min_ahead_hours=args.min_ahead,
                             slot=job.get("slot") or args.slot or None)
        item = build_item(
            url, title=job.get("title") or args.title, hook=job.get("hook") or args.hook,
            name=job.get("name") or args.name, slot_iso=iso,
            words=job.get("words") or args.words,
        )
        if args.no_publish or job.get("no_publish"):
            item["no_publish"] = True
            item["demo"] = True
        path = append_queue_item(item, root)
        record_request(url, item, status="queued", root=root)
        ids.append(item["id"])
        print(f"queued {item['id']} slot={item['slot']} file={path}")
    print("ids=" + ",".join(ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
