#!/usr/bin/env python3
"""Pre-rendered, QC-passed backup Shorts that can be assigned to a slot instantly.

A ready stock video has already been rendered and Gemini-QC-passed, lives on the
`backup-stock` GitHub release, and has no slot until it is assigned. Health
checks every official IST slot (and any extra queue slot). If a live item fails
or QC-rejects, or an official slot is missing from the queue entirely, the next
unused stock video is assigned immediately. A queued item that is still
rendering is given until ~60 minutes before the slot. Buffer then posts it at
the slot time (or shareNow if late).
Used entries are never posted twice. Whenever ready stock is below the
target of 30, health dispatches a few new stock renders per run (evergreen
sources-release topics only, 2 per run, own concurrency group) until 30 is
reached again.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
MANIFEST = "config/backup_stock.json"
STOCK_RE = re.compile(r"^stock-(.+)-(\d{2})$")
TARGET = 30
REFILL_AT = 5
MAX_REFILL = 2
RELEASE = "backup-stock"
LEAD_MIN = 60
PENDING_HOURS = 3
PAST_HOURS = 6
TITLE_LOOKBACK_DAYS = 7
OFFICIAL_FALLBACK = ["06:30", "09:00", "11:30", "13:30", "16:00", "18:30", "20:30", "22:30"]

try:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import slot_times as _slot_times  # noqa: E402
except Exception:
    _slot_times = None


def official_times() -> list[str]:
    if _slot_times is not None:
        return list(_slot_times.official_times())
    return list(OFFICIAL_FALLBACK)


def official_iso(day: str, hhmm: str) -> str:
    if _slot_times is not None:
        return _slot_times.slot_iso(day, hhmm)
    return f"{day}T{hhmm}:00+05:30"


def _repo() -> Path:
    return Path(os.environ.get("SKY_ROOT") or REPO)


def now_ist() -> datetime:
    return datetime.now(IST)


def load_manifest(root: Path | None = None) -> dict:
    root = root or _repo()
    p = root / MANIFEST
    if not p.exists():
        return {
            "target": TARGET,
            "refill_at": REFILL_AT,
            "max_refill_per_run": MAX_REFILL,
            "release": RELEASE,
            "items": [],
        }
    try:
        d = json.loads(p.read_text())
    except Exception:
        d = {}
    d.setdefault("target", TARGET)
    d.setdefault("refill_at", REFILL_AT)
    d.setdefault("max_refill_per_run", MAX_REFILL)
    d.setdefault("release", RELEASE)
    d.setdefault("items", [])
    return d


def save_manifest(man: dict, root: Path | None = None) -> None:
    root = root or _repo()
    p = root / MANIFEST
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n")


def load_pool(root: Path | None = None) -> list[dict]:
    root = root or _repo()
    try:
        return json.loads((root / "config" / "backup_pool.json").read_text()).get("items") or []
    except Exception:
        return []


def has_preuploaded(it: dict) -> bool:
    """True when the pool topic has at least one official scene clip (never a trailer)."""
    try:
        import sourcing
        return sourcing.item_has_scene_source(it)
    except Exception:
        fa = (it.get("fallback_asset") or "") + " " + " ".join(it.get("fallback_mp4") or [])
        if not (it.get("fallback_asset") or it.get("fallback_mp4")):
            return False
        return "trailer" not in fa.lower() and "teaser" not in fa.lower()


def parse_stock_id(iid: str) -> tuple[str, int] | None:
    m = STOCK_RE.match(iid or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


def result_path(root: Path, iid: str) -> Path:
    return root / "results" / f"{iid}.json"


def read_result(root: Path, iid: str) -> dict | None:
    p = result_path(root, iid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def write_result(root: Path, r: dict) -> None:
    iid = r["id"]
    (root / "results").mkdir(parents=True, exist_ok=True)
    result_path(root, iid).write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")


def iter_results(root: Path) -> list[dict]:
    out = []
    for p in glob.glob(str(root / "results" / "*.json")):
        try:
            out.append(json.loads(Path(p).read_text()))
        except Exception:
            continue
    return out


def pool_slug(b: dict) -> str:
    bid = b.get("id") or ""
    return bid[len("backup-"):] if bid.startswith("backup-") else bid


def qc_was_skipped(r: dict | None) -> bool:
    """True when Gemini QC was skipped, errored, or never recorded as a real verdict."""
    if not r:
        return False
    if r.get("qc_status") == "skipped" or r.get("qc_pending"):
        return True
    g = r.get("gate") if isinstance(r.get("gate"), dict) else {}
    qc = g.get("gemini_qc")
    if isinstance(qc, str) and "skipped" in qc.lower():
        return True
    if r.get("audit_hold") and "qc_skipped" in (r.get("audit_hold") or []):
        return True
    return False


def source_topic_mismatch(r: dict | None, ent: dict | None = None) -> bool:
    """True when the footage topic does not match the title/slug (e.g. Blue Lock under a Vinland title)."""
    if not r:
        return False
    if r.get("audit_hold") and "topic_mismatch" in (r.get("audit_hold") or []):
        return True
    import sourcing
    src = r.get("source") or ""
    if not src:
        return False
    probe = {
        "id": (ent or {}).get("id") or r.get("id") or "",
        "title": r.get("title") or (ent or {}).get("title") or "",
        "words": r.get("words") or "",
        "fallback_asset": sourcing.source_filename(src),
        "fallback_mp4": [src],
        "stock_topic": (ent or {}).get("topic_id") or r.get("stock_topic") or "",
        "tags": r.get("tags") or [],
    }
    # Prefer the stock/entry slug as the expected topic.
    slug = (ent or {}).get("slug") or _slug_of(r)
    if slug:
        probe["fallback_asset"] = slug + "_trailer_h264.mp4"
        if not sourcing.source_matches_topic({"id": f"stock-{slug}-01", "fallback_asset": slug + "_scene1_h264.mp4",
                                              "stock_topic": f"backup-{slug}"}, src):
            # also compare against the result title's topic words
            title = (r.get("title") or "").lower()
            src_slug = sourcing.source_topic_slug(src)
            compact = re.sub(r"[^a-z0-9]+", "", title)
            if src_slug and src_slug in compact:
                return False
            if slug and (src_slug == slug or src_slug.startswith(slug) or slug.startswith(src_slug)):
                return False
            return True
        return False
    return not sourcing.source_matches_topic(probe, src)


def is_ready_result(r: dict | None) -> bool:
    if not r:
        return False
    if r.get("status") != "done" or not r.get("asset_url"):
        return False
    if r.get("no_publish") or r.get("demo"):
        return False
    if "/test-renders/" in str(r.get("asset_url") or ""):
        return False
    if r.get("stock_ready") is False:
        return False
    if qc_was_skipped(r) or source_topic_mismatch(r):
        return False
    try:
        import sourcing
        if sourcing.is_trailer_result(r) or sourcing.is_trailer_source(str(r.get("source") or "")):
            return False
    except Exception:
        src = str(r.get("source") or "")
        if "trailer" in src.lower() or "teaser" in src.lower():
            return False
    return True


def norm_title(t: str) -> str:
    return " ".join((t or "").lower().split())


def is_title_match_miss(r: dict, slot_s: str | None = None) -> bool:
    """True when publish recorded an existing Buffer post by title that is not this slot.

    Buffer's title-match idempotency swallowed 15 Nov 13:30 (same Avatar title as
    14 Nov 12:30). A match whose dueAt/sentAt is this slot is real coverage.
    """
    if (r.get("buffer_note") or "") != "matched existing post by title":
        return False
    slot_s = slot_s or r.get("slot")
    due = r.get("buffer_due_at") or r.get("buffer_posted_at")
    if not slot_s or not due:
        return True
    try:
        slot = datetime.fromisoformat(slot_s)
        due_dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
        if due_dt.tzinfo is None:
            due_dt = due_dt.replace(tzinfo=timezone.utc)
        return abs((due_dt - slot).total_seconds()) > 15 * 60
    except Exception:
        return True


def slot_is_covered(root: Path, slot_s: str) -> bool:
    """True when a publishable done video already belongs to this slot."""
    for r in iter_results(root):
        if r.get("slot") != slot_s:
            continue
        if is_ready_result(r) and not is_title_match_miss(r, slot_s):
            return True
    return False


def slot_post_conflicts(r: dict, rows: list[dict]) -> bool:
    """Skip posting r when another result already covers the same slot."""
    slot = r.get("slot")
    if not slot:
        return False
    iid = r.get("id")
    for o in rows:
        if o.get("id") == iid or o.get("slot") != slot:
            continue
        if is_title_match_miss(o, slot):
            continue
        if o.get("buffer_post_id"):
            return True
        if o.get("assigned_from_stock") and not r.get("assigned_from_stock"):
            return True
    return False


def topics_used_on_day(root: Path, day: str) -> set[str]:
    """Slugs already queued, rendered, or assigned on this IST day."""
    used = set()
    prefix = day.replace("-", "")
    for r in iter_results(root):
        slot = r.get("slot") or ""
        iid = r.get("id") or ""
        if not (slot.startswith(day) or iid.startswith(prefix)):
            continue
        if r.get("no_publish") or r.get("demo"):
            continue
        slug = _slug_of(r)
        if slug:
            used.add(slug)
    qdir = root / "queue"
    if qdir.exists():
        qp = qdir / f"{day}.json"
        if qp.exists():
            try:
                for it in json.loads(qp.read_text()):
                    if it.get("status") in ("replaced", "retired"):
                        continue
                    slug = _slug_of(it)
                    if slug:
                        used.add(slug)
            except Exception:
                pass
    return used


def _parse_when(s: str) -> datetime | None:
    if not s:
        return None
    try:
        raw = str(s).replace(" IST", "").replace("Z", "+00:00")
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=IST)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except Exception:
        return None


def recent_titles_and_topics(root: Path, now: datetime | None = None,
                             days: int = TITLE_LOOKBACK_DAYS,
                             buffer_posts: list | None = None) -> tuple[set[str], set[str]]:
    """Titles and topic slugs sent or scheduled in the last `days` (results + Buffer)."""
    now = now or now_ist()
    start = now - timedelta(days=days)
    end = now + timedelta(days=1)
    titles: set[str] = set()
    slugs: set[str] = set()

    def _in_window(dt: datetime | None) -> bool:
        return bool(dt and start <= dt <= end)

    for r in iter_results(root):
        if r.get("no_publish") or r.get("demo"):
            continue
        posted = (r.get("buffer_status") in ("sent", "scheduled")
                  or r.get("buffer_post_id") or r.get("youtube_url")
                  or r.get("assigned_from_stock") or r.get("assigned_slot"))
        if not posted:
            continue
        when = None
        for key in ("buffer_posted_at", "buffer_due_at", "buffer_created_at",
                    "used_at", "slot", "assigned_slot"):
            when = _parse_when(r.get(key) or "")
            if when:
                break
        if not _in_window(when):
            continue
        nt = norm_title(r.get("title") or "")
        if nt:
            titles.add(nt)
        slug = _slug_of(r)
        if slug:
            slugs.add(slug)

    posts = buffer_posts
    if posts is None and os.environ.get("BUFFER_API_KEY"):
        try:
            import buffer_post  # noqa: WPS433
            posts = buffer_post.channel_posts()
        except Exception as e:
            print(f"buffer title scan skipped: {e}", file=sys.stderr)
            posts = None
    for x in posts or []:
        if (x.get("status") or "") not in ("sent", "scheduled"):
            continue
        when = _parse_when(x.get("sentAt") or "") or _parse_when(x.get("dueAt") or "")
        if when and not _in_window(when):
            continue
        if not when:
            # no timestamp: still treat as recent so a title collision cannot sneak through
            pass
        nt = norm_title(((x.get("metadata") or {}).get("title") or ""))
        if nt:
            titles.add(nt)
    return titles, slugs


def _slug_of(r: dict) -> str:
    if r.get("stock_topic"):
        t = r["stock_topic"]
        return t[len("backup-"):] if t.startswith("backup-") else t
    if r.get("backup_item"):
        t = r["backup_item"]
        return t[len("backup-"):] if t.startswith("backup-") else t
    parsed = parse_stock_id(r.get("id") or "")
    if parsed:
        return parsed[0]
    iid = r.get("id") or ""
    parts = iid.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return "-".join(parts[2:])
    if iid.startswith("backup-"):
        return iid[len("backup-"):]
    return ""


def _entry_status(ent: dict, root: Path, now: datetime) -> str:
    """Reconcile a manifest row against its result file."""
    iid = ent.get("id") or ""
    r = read_result(root, iid)
    if ent.get("assigned_slot") or (r and (r.get("stock_used") or r.get("assigned_slot"))):
        return "used"
    if r and r.get("status") == "failed":
        return "failed"
    if is_ready_result(r) and not (r or {}).get("stock_used"):
        return "ready"
    if r and r.get("status") == "done" and not (r or {}).get("stock_used"):
        return "not_ready"
    if r and r.get("status") in ("retry_queued",):
        return "pending"
    if ent.get("status") == "pending":
        at = ent.get("queued_at") or ""
        try:
            dt = datetime.strptime(at.replace(" IST", ""), "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            if now - dt < timedelta(hours=PENDING_HOURS):
                return "pending"
        except Exception:
            return "pending"
    return ent.get("status") or "pending"


def sync(root: Path | None = None, now: datetime | None = None) -> dict:
    """Refresh manifest statuses from results/stock-*.json. Never deletes videos."""
    root = root or _repo()
    now = now or now_ist()
    man = load_manifest(root)
    by_id = {e.get("id"): e for e in man.get("items") or [] if e.get("id")}
    for p in sorted((root / "results").glob("stock-*.json")):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        iid = r.get("id") or p.stem
        parsed = parse_stock_id(iid)
        if not parsed:
            continue
        slug, _n = parsed
        ent = by_id.get(iid) or {"id": iid, "topic_id": f"backup-{slug}", "slug": slug}
        ent["result_id"] = iid
        if r.get("asset_url"):
            ent["asset_url"] = r["asset_url"]
        if r.get("assigned_slot"):
            ent["assigned_slot"] = r["assigned_slot"]
            ent["assigned_result"] = r.get("assigned_result")
            ent["used_at"] = r.get("used_at") or ent.get("used_at")
        ent["status"] = _entry_status(ent, root, now)
        by_id[iid] = ent
    for ent in by_id.values():
        ent["status"] = _entry_status(ent, root, now)
    man["items"] = sorted(by_id.values(), key=lambda e: e.get("id") or "")
    save_manifest(man, root)
    return man


def ready_entries(man: dict) -> list[dict]:
    return [e for e in man.get("items") or [] if e.get("status") == "ready" and e.get("asset_url")]


def has_ready_stock(root: Path | None = None) -> bool:
    """Read-only: True when at least one unused, publishable stock video exists."""
    root = root or _repo()
    man = load_manifest(root)
    for e in ready_entries(man):
        iid = e.get("id") or ""
        if e.get("assigned_slot"):
            continue
        r = read_result(root, iid)
        if is_ready_result(r) and not (r or {}).get("stock_used") and not (r or {}).get("assigned_slot"):
            return True
    for r in iter_results(root):
        if not parse_stock_id(r.get("id") or ""):
            continue
        if is_ready_result(r) and not r.get("stock_used") and not r.get("assigned_slot"):
            return True
    return False


def pending_entries(man: dict) -> list[dict]:
    return [e for e in man.get("items") or [] if e.get("status") == "pending"]


def pick_stock(man: dict, root: Path, day: str, now: datetime | None = None,
               buffer_posts: list | None = None) -> dict | None:
    ready = ready_entries(man)
    if not ready:
        return None
    now = now or now_ist()
    used = topics_used_on_day(root, day)
    recent_titles, recent_slugs = recent_titles_and_topics(root, now, buffer_posts=buffer_posts)
    used |= recent_slugs

    def conflicts(e: dict) -> bool:
        if (e.get("slug") or "") in used:
            return True
        src = read_result(root, e.get("id") or "") or {}
        try:
            import sourcing
            if sourcing.is_trailer_result(src) or sourcing.is_trailer_source(str(src.get("source") or "")):
                return True
        except Exception:
            if "trailer" in str(src.get("source") or "").lower():
                return True
        # Same song must not go out twice in 3 days, including pre-rendered stock.
        try:
            import music_lib
            music = src.get("music") or ""
            if music:
                recent = set(music_lib.recent_excludes(root / "results", root, days=3, now=now))
                if music in recent or any(music_lib._norm_music(music) == music_lib._norm_music(x) for x in recent):
                    return True
        except Exception:
            pass
        title = norm_title(src.get("title") or e.get("title") or "")
        return bool(title and title in recent_titles)

    prefer = [e for e in ready if not conflicts(e)]
    # Never fall back to a title/topic Buffer would swallow (same-title miss).
    return prefer[0] if prefer else None


def assign_to_slot(root: Path, slot_s: str, now: datetime | None = None) -> dict | None:
    """Copy a ready stock result onto this slot and mark the stock used."""
    root = root or _repo()
    now = now or now_ist()
    if slot_is_covered(root, slot_s):
        return None
    man = sync(root, now)
    try:
        slot = datetime.fromisoformat(slot_s)
        day = slot.date().isoformat()
        code = slot.strftime("%Y%m%d-%H%M")
    except Exception:
        return None
    ent = pick_stock(man, root, day, now=now)
    if not ent:
        return None
    stock_id = ent["id"]
    src = read_result(root, stock_id)
    if not is_ready_result(src):
        return None
    slug = ent.get("slug") or _slug_of(src) or stock_id
    new_id = f"{code}-{slug}"
    if result_path(root, new_id).exists():
        existing = read_result(root, new_id) or {}
        if is_ready_result(existing):
            return None
        new_id = f"{code}-stock-{slug}"
    dest = dict(src)
    for k in ("buffer_post_id", "buffer_status", "buffer_due_at", "buffer_posted_at",
              "buffer_created_at", "buffer_edited_at", "buffer_error", "buffer_note",
              "youtube_url", "stock_used", "assigned_slot", "assigned_result", "used_at"):
        dest.pop(k, None)
    dest["id"] = new_id
    dest["slot"] = slot_s
    dest["stock_id"] = stock_id
    dest["stock_topic"] = ent.get("topic_id") or src.get("stock_topic")
    dest["backup_item"] = ent.get("topic_id") or src.get("stock_topic")
    dest["assigned_from_stock"] = True
    dest["at"] = now.strftime("%Y-%m-%d %H:%M IST")
    dest["detail"] = (src.get("detail") or "stock") + f" (assigned from {stock_id})"
    write_result(root, dest)

    src["stock_used"] = True
    src["assigned_slot"] = slot_s
    src["assigned_result"] = new_id
    src["used_at"] = now.strftime("%Y-%m-%d %H:%M IST")
    write_result(root, src)

    ent["status"] = "used"
    ent["assigned_slot"] = slot_s
    ent["assigned_result"] = new_id
    ent["used_at"] = src["used_at"]
    for i, e in enumerate(man["items"]):
        if e.get("id") == stock_id:
            man["items"][i] = ent
            break
    save_manifest(man, root)
    return dest


def queue_items(root: Path) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(root / "queue" / "2026-*.json"))):
        try:
            out.extend(json.load(open(f)))
        except Exception:
            pass
    return out


def _live_for_slot(items: list[dict], slot_s: str) -> list[dict]:
    return [it for it in items if it.get("slot") == slot_s and it.get("status", "queued") == "queued"]


def slot_needs_stock(root: Path, slot_s: str, now: datetime | None = None,
                     lead_min: int = LEAD_MIN, past_h: float = PAST_HOURS) -> bool:
    """Assign stock when the slot is failed, or still empty ~60 min out / late."""
    now = now or now_ist()
    if slot_is_covered(root, slot_s):
        return False
    try:
        slot = datetime.fromisoformat(slot_s)
    except Exception:
        return False
    if slot - now > timedelta(hours=24) or now - slot > timedelta(hours=past_h):
        return False
    items = _live_for_slot(queue_items(root), slot_s)
    results = {it["id"]: read_result(root, it["id"]) for it in items}

    def st(it):
        r = results.get(it["id"]) or {}
        if r.get("qc_rejected"):
            return "failed"
        return r.get("status")

    all_failed = bool(items) and all(st(it) == "failed" for it in items)
    no_item = not items
    imminent = timedelta(minutes=-past_h * 60) <= slot - now <= timedelta(minutes=lead_min)
    if all_failed:
        return True
    if no_item:
        # Official (or leftover) slot with nothing in the queue: do not wait
        # until T-60. A missed health run in that narrow window is how 18:30
        # on 14 Nov was skipped.
        return True
    if imminent:
        # Queued item still has no publishable video, and the slot is close
        # (or already late).
        return True
    return False


def upcoming_slots(root: Path, now: datetime | None = None, past_h: float = PAST_HOURS) -> list[str]:
    now = now or now_ist()
    horizon = now + timedelta(hours=24)
    slots = set()
    for it in queue_items(root):
        s = it.get("slot")
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            continue
        if now - timedelta(hours=past_h) <= dt <= horizon:
            slots.add(s)
    day0 = now.date()
    for d in range(0, 3):
        day = (day0 + timedelta(days=d)).isoformat()
        for hhmm in official_times():
            iso = official_iso(day, hhmm)
            try:
                dt = datetime.fromisoformat(iso)
            except Exception:
                continue
            if now - timedelta(hours=past_h) <= dt <= horizon:
                slots.add(iso)
    return sorted(slots)


def assign_needed(root: Path | None = None, now: datetime | None = None) -> list[dict]:
    root = root or _repo()
    now = now or now_ist()
    sync(root, now)
    assigned = []
    for slot_s in upcoming_slots(root, now):
        if not slot_needs_stock(root, slot_s, now):
            continue
        got = assign_to_slot(root, slot_s, now)
        if got:
            assigned.append(got)
            print(f"assigned {got['stock_id']} -> {got['id']} for {slot_s}", file=sys.stderr)
    return assigned


def next_refill_ids(root: Path | None = None, limit: int | None = None,
                    now: datetime | None = None) -> list[str]:
    """New stock-* ids to render this run, or [] if stock is healthy."""
    root = root or _repo()
    now = now or now_ist()
    man = sync(root, now)
    target = int(man.get("target") or TARGET)
    cap = int(limit if limit is not None else man.get("max_refill_per_run") or MAX_REFILL)
    ready = ready_entries(man)
    pending = pending_entries(man)
    # Keep filling (capped per run) until ready+pending reaches the target of 30.
    # refill_at is kept in the manifest for compatibility but no longer gates this.
    need = target - len(ready) - len(pending)
    if need <= 0 or cap <= 0:
        return []
    pool = [b for b in load_pool(root) if has_preuploaded(b)]
    if not pool:
        return []
    existing_ids = {e.get("id") for e in man.get("items") or []}
    counts: dict[str, int] = {}
    max_n: dict[str, int] = {}
    for e in man.get("items") or []:
        slug = e.get("slug") or ""
        parsed = parse_stock_id(e.get("id") or "")
        if parsed:
            slug, n = parsed
            max_n[slug] = max(max_n.get(slug, 0), n)
        if e.get("status") in ("ready", "pending"):
            counts[slug] = counts.get(slug, 0) + 1
    # Also count leftover result files not yet in the manifest
    for r in iter_results(root):
        parsed = parse_stock_id(r.get("id") or "")
        if not parsed:
            continue
        slug, n = parsed
        max_n[slug] = max(max_n.get(slug, 0), n)
        existing_ids.add(r["id"])
    picks = []
    # Rotate topics with the fewest in-stock copies first for variety.
    order = sorted(pool, key=lambda b: (counts.get(pool_slug(b), 0), pool_slug(b)))
    if not order:
        return []
    i = 0
    guard = 0
    while len(picks) < min(need, cap) and guard < 200:
        guard += 1
        b = order[i % len(order)]
        i += 1
        slug = pool_slug(b)
        n = max_n.get(slug, 0) + 1
        iid = f"stock-{slug}-{n:02d}"
        if iid in existing_ids:
            max_n[slug] = n
            continue
        picks.append(iid)
        existing_ids.add(iid)
        max_n[slug] = n
        counts[slug] = counts.get(slug, 0) + 1
        # re-sort occasionally so one topic does not eat the batch
        if i % len(order) == 0:
            order = sorted(pool, key=lambda b: (counts.get(pool_slug(b), 0), pool_slug(b)))
    return picks


def mark_pending(ids: list[str], root: Path | None = None, now: datetime | None = None) -> dict:
    root = root or _repo()
    now = now or now_ist()
    man = sync(root, now)
    by_id = {e.get("id"): e for e in man.get("items") or []}
    ts = now.strftime("%Y-%m-%d %H:%M IST")
    for iid in ids:
        parsed = parse_stock_id(iid)
        if not parsed:
            continue
        slug, _n = parsed
        ent = by_id.get(iid) or {"id": iid, "topic_id": f"backup-{slug}", "slug": slug}
        if ent.get("status") in ("ready", "used"):
            continue
        ent["status"] = "pending"
        ent["queued_at"] = ts
        by_id[iid] = ent
    man["items"] = sorted(by_id.values(), key=lambda e: e.get("id") or "")
    save_manifest(man, root)
    return man


def audit_ready_stock(root: Path | None = None, now: datetime | None = None) -> list[dict]:
    """Mark ready stock not-ready when the source topic mismatches the title or QC was skipped.

    Never deletes videos or release assets. Stamps the result and manifest only.
    """
    root = root or _repo()
    now = now or now_ist()
    man = sync(root, now)
    held = []
    for ent in man.get("items") or []:
        r = read_result(root, ent.get("id") or "")
        # Inspect anything that looks ready, plus already-done unused clips.
        if not r:
            continue
        if (ent.get("status") not in ("ready", "not_ready")
                and not (r.get("status") == "done" and r.get("asset_url") and not r.get("stock_used"))):
            continue
        if r.get("stock_used") or ent.get("assigned_slot"):
            continue
        reasons = []
        if qc_was_skipped(r):
            reasons.append("qc_skipped")
        if source_topic_mismatch(r, ent):
            reasons.append("topic_mismatch")
        try:
            import sourcing
            if sourcing.is_trailer_result(r) or sourcing.is_trailer_source(str(r.get("source") or "")):
                reasons.append("trailer_source")
        except Exception:
            if "trailer" in str(r.get("source") or "").lower():
                reasons.append("trailer_source")
        if not reasons:
            continue
        ent["status"] = "not_ready"
        ent["audit_hold"] = reasons
        r["stock_ready"] = False
        r["audit_hold"] = reasons
        # Keep status=done so the video is not deleted or re-rendered as failed.
        rp = root / "results" / f"{ent['id']}.json"
        rp.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
        held.append({"id": ent.get("id"), "reasons": reasons, "title": r.get("title"),
                     "source": r.get("source")})
    man["items"] = sorted({e.get("id"): e for e in (man.get("items") or []) if e.get("id")}.values(),
                          key=lambda e: e.get("id") or "")
    # re-apply statuses after stamps
    for ent in man.get("items") or []:
        if ent.get("id") in {h["id"] for h in held}:
            ent["status"] = "not_ready"
    save_manifest(man, root)
    return held


def status_summary(root: Path | None = None, now: datetime | None = None) -> dict:
    root = root or _repo()
    now = now or now_ist()
    man = sync(root, now)
    ready = ready_entries(man)
    pending = pending_entries(man)
    used = [e for e in man.get("items") or [] if e.get("status") == "used"]
    return {
        "ready": len(ready),
        "pending": len(pending),
        "used": len(used),
        "target": int(man.get("target") or TARGET),
        "refill_at": int(man.get("refill_at") or REFILL_AT),
        "need_refill": (len(ready) + len(pending)) < int(man.get("target") or TARGET),
        "ready_ids": [e["id"] for e in ready],
        "pending_ids": [e["id"] for e in pending],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = (argv[0] if argv else "status").replace("_", "-")
    root = _repo()
    now = now_ist()
    if cmd == "assign":
        got = assign_needed(root, now)
        print(json.dumps({"assigned": [g["id"] for g in got], "n": len(got)}))
        return 0
    if cmd in ("refill-ids", "refill_ids"):
        cap = int(argv[1]) if len(argv) > 1 else MAX_REFILL
        ids = next_refill_ids(root, limit=cap, now=now)
        if ids:
            mark_pending(ids, root, now)
        line = f"ids={json.dumps(ids)}\ncount={len(ids)}\nids_csv={','.join(ids)}\n"
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            open(out, "a").write(line)
        print(line, end="")
        return 0
    # status
    s = status_summary(root, now)
    print(json.dumps(s, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
