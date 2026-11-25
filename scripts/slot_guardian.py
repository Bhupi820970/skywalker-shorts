#!/usr/bin/env python3
"""Guard official IST slots so none are missed.

Before each official slot (and any extra queue slot in a short lead window),
verify a Buffer post is scheduled or already sent for that minute. If not:

  1. If a publishable result already exists for the slot, flag it for publish.yml.
  2. Else assign a ready pre-rendered stock video (pool kept at >= MIN_READY=4).
  3. Last resort: assign a never-posted evergreen ready video (same title or
     asset as an already-posted Short is skipped).

Demo / no_publish / test-renders items are never posted. Videos are never deleted.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import backup_stock  # noqa: E402
import slot_times  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
LEAD_MIN = 45
PAST_MIN = 20
MIN_READY = backup_stock.MIN_READY


def _parse_dt(s):
    if not s:
        return None
    try:
        raw = str(s).replace(" IST", "").replace("Z", "+00:00")
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except Exception:
        return None


def buffer_covers_slot(posts: list | None, slot_s: str, slack_s: float = 90) -> bool:
    """True when Buffer already has a scheduled or sent post at this slot minute."""
    slot = _parse_dt(slot_s)
    if not slot or not posts:
        return False
    for x in posts:
        if (x.get("status") or "") not in ("sent", "scheduled"):
            continue
        dt = _parse_dt(x.get("dueAt") or "") or _parse_dt(x.get("sentAt") or "")
        if dt and abs((dt - slot).total_seconds()) <= slack_s:
            return True
    return False


def result_posted_for_slot(root: Path, slot_s: str) -> dict | None:
    for r in backup_stock.iter_results(root):
        if r.get("slot") != slot_s:
            continue
        if backup_stock.must_not_publish(r):
            continue
        if r.get("buffer_post_id") and (r.get("buffer_status") or "") in (
            "sent", "scheduled", "posted",
        ):
            return r
        if r.get("youtube_url") and r.get("buffer_status") in ("sent", "scheduled", "posted"):
            return r
    return None


def publishable_for_slot(root: Path, slot_s: str) -> dict | None:
    for r in backup_stock.iter_results(root):
        if r.get("slot") != slot_s:
            continue
        if backup_stock.must_not_publish(r):
            continue
        if backup_stock.is_ready_result(r) and not backup_stock.is_title_match_miss(r, slot_s):
            return r
    return None


def imminent_slots(root: Path, now: datetime, lead_min: int = LEAD_MIN,
                   past_min: int = PAST_MIN) -> list[str]:
    """Official IST slots (plus leftover queue slots) in the guard window."""
    start = now - timedelta(minutes=past_min)
    end = now + timedelta(minutes=lead_min)
    slots: set[str] = set()
    day0 = now.date()
    for d in range(-1, 2):
        day = (day0 + timedelta(days=d)).isoformat()
        for hhmm in backup_stock.official_times():
            iso = backup_stock.official_iso(day, hhmm)
            dt = _parse_dt(iso)
            if dt and start <= dt <= end:
                slots.add(iso)
    for it in backup_stock.queue_items(root):
        s = it.get("slot")
        dt = _parse_dt(s)
        if dt and start <= dt <= end:
            slots.add(s)
    return sorted(slots)


def posted_keys(root: Path, posts: list | None) -> tuple[set[str], set[str]]:
    """Titles and asset URLs that have already been posted (never duplicate)."""
    titles: set[str] = set()
    assets: set[str] = set()
    for r in backup_stock.iter_results(root):
        if backup_stock.must_not_publish(r):
            continue
        posted = bool(r.get("buffer_post_id") or r.get("youtube_url") or r.get("stock_used"))
        if posted:
            t = backup_stock.norm_title(r.get("title") or "")
            if t:
                titles.add(t)
            url = str(r.get("asset_url") or "")
            if url:
                assets.add(url)
    for x in posts or []:
        if (x.get("status") or "") not in ("sent", "scheduled"):
            continue
        t = backup_stock.norm_title(((x.get("metadata") or {}).get("title")) or "")
        if t:
            titles.add(t)
    return titles, assets


def pick_evergreen(root: Path, day: str, now: datetime,
                   posts: list | None = None) -> dict | None:
    """Last-resort never-posted ready video. Skips titles/assets already on the channel."""
    man = backup_stock.load_manifest(root)
    ent = backup_stock.pick_stock(man, root, day, now=now, buffer_posts=posts)
    if ent:
        src = backup_stock.read_result(root, ent.get("id") or "")
        if src and not backup_stock.must_not_publish(src):
            return ent
    titles, assets = posted_keys(root, posts)
    import posted_history
    rows = backup_stock.iter_results(root)
    hist = posted_history.index(root, rows)
    held = posted_history.reserved_clips([r for r in rows if r.get("assigned_from_stock")])
    # Broader last resort: any unused ready stock, even same-day topic,
    # as long as the video itself has never been posted.
    for e in backup_stock.ready_entries(man):
        if e.get("assigned_slot"):
            continue
        src = backup_stock.read_result(root, e.get("id") or "") or {}
        if not backup_stock.is_ready_result(src) or src.get("stock_used"):
            continue
        if backup_stock.must_not_publish(src):
            continue
        title = backup_stock.norm_title(src.get("title") or e.get("title") or "")
        url = str(src.get("asset_url") or e.get("asset_url") or "")
        if title and title in titles:
            continue
        if url and url in assets:
            continue
        # Never a source scene / video already on the channel or waiting in another slot.
        if posted_history.conflict(src, hist) or set(posted_history.result_clips(src)) & held:
            continue
        try:
            import sourcing
            if sourcing.is_trailer_result(src) or sourcing.is_trailer_source(str(src.get("source") or "")):
                continue
        except Exception:
            if "trailer" in str(src.get("source") or "").lower():
                continue
        return e
    return None


def load_buffer_posts():
    try:
        import buffer_post
        return buffer_post.channel_posts()
    except SystemExit as e:
        print(f"slot-guardian: Buffer unavailable ({e}); using results only", file=sys.stderr)
        return None
    except Exception as e:
        print(f"slot-guardian: Buffer fetch failed ({e}); using results only", file=sys.stderr)
        return None


def guard(root: Path | None = None, now: datetime | None = None,
          posts: list | None = None, *, fetch_buffer: bool = False,
          lead_min: int = LEAD_MIN, past_min: int = PAST_MIN) -> dict:
    root = root or backup_stock._repo()
    now = now or backup_stock.now_ist()
    backup_stock.retire_broken_topics(root, now)
    backup_stock.sync(root, now)
    if fetch_buffer and posts is None:
        posts = load_buffer_posts()
    posts = posts or []

    assigned = []
    covered = []
    need_publish = []
    uncovered = []

    for slot_s in imminent_slots(root, now, lead_min=lead_min, past_min=past_min):
        if buffer_covers_slot(posts, slot_s) or result_posted_for_slot(root, slot_s):
            covered.append({"slot": slot_s, "how": "buffer"})
            continue
        existing = publishable_for_slot(root, slot_s)
        if existing:
            need_publish.append(existing["id"])
            covered.append({"slot": slot_s, "how": "publish_existing", "id": existing["id"]})
            continue
        got = backup_stock.assign_to_slot(root, slot_s, now)
        if got and not backup_stock.must_not_publish(got):
            assigned.append(got)
            need_publish.append(got["id"])
            covered.append({"slot": slot_s, "how": "assigned_stock", "id": got["id"]})
            continue
        # Last resort evergreen (never a video already posted).
        try:
            day = datetime.fromisoformat(slot_s).date().isoformat()
        except Exception:
            day = now.date().isoformat()
        ent = pick_evergreen(root, day, now, posts)
        if ent and not backup_stock.slot_is_covered(root, slot_s):
            # assign_to_slot will pick_stock again; if that returned None due to
            # same-day conflict, copy the evergreen video ourselves via a
            # temporary preference: mark others not ready? Safer to call a
            # direct assign of this entry.
            got = _assign_entry(root, ent, slot_s, now)
            if got and not backup_stock.must_not_publish(got):
                assigned.append(got)
                need_publish.append(got["id"])
                covered.append({"slot": slot_s, "how": "evergreen", "id": got["id"]})
                continue
        uncovered.append(slot_s)

    stock = backup_stock.status_summary(root, now)
    report = {
        "at": now.strftime("%Y-%m-%d %H:%M IST"),
        "slots": covered,
        "assigned": [g["id"] for g in assigned],
        "need_publish": list(dict.fromkeys(need_publish)),
        "uncovered": uncovered,
        "stock_ready": stock["ready"],
        "min_ready": MIN_READY,
        "below_ready_floor": stock.get("below_ready_floor"),
        "need_refill": bool(stock.get("below_ready_floor") or stock.get("need_refill")),
        "retired": stock.get("retired") or [],
        "ok": not uncovered,
    }
    return report


def _assign_entry(root: Path, ent: dict, slot_s: str, now: datetime) -> dict | None:
    """Assign a specific ready stock entry to a slot (last-resort evergreen path)."""
    if backup_stock.slot_is_covered(root, slot_s):
        return None
    src = backup_stock.read_result(root, ent.get("id") or "")
    if not backup_stock.is_ready_result(src) or backup_stock.must_not_publish(src):
        return None
    # Reuse assign_to_slot's copy logic by temporarily making this the only pick.
    man = backup_stock.load_manifest(root)
    orig = backup_stock.pick_stock

    def _only(man_, root_, day, now=None, buffer_posts=None):
        return ent

    backup_stock.pick_stock = _only  # type: ignore
    try:
        return backup_stock.assign_to_slot(root, slot_s, now)
    finally:
        backup_stock.pick_stock = orig


def write_github_output(report: dict) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    assigned = ",".join(report.get("assigned") or [])
    need_pub = ",".join(report.get("need_publish") or [])
    lines = [
        f"assigned={assigned}",
        f"need_publish={'1' if report.get('need_publish') else '0'}",
        f"need_publish_ids={need_pub}",
        f"need_refill={'1' if report.get('need_refill') and report.get('below_ready_floor') else '0'}",
        f"uncovered={','.join(report.get('uncovered') or [])}",
    ]
    with open(out, "a") as f:
        f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    root = backup_stock._repo()
    now = backup_stock.now_ist()
    report = guard(root, now, fetch_buffer=True)
    write_github_output(report)
    print(json.dumps({k: report[k] for k in (
        "at", "assigned", "need_publish", "uncovered", "stock_ready",
        "below_ready_floor", "need_refill", "ok",
    )}, indent=2))
    status_path = root / "config" / "slot_guardian.json"
    # Status file is ephemeral / optional; health may commit assignments only.
    try:
        status_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # Non-zero only when an imminent official slot could not be covered at all.
    return 1 if report.get("uncovered") else 0


if __name__ == "__main__":
    raise SystemExit(main())
