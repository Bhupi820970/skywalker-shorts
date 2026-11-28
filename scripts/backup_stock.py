#!/usr/bin/env python3
"""Pre-rendered, QC-passed backup Shorts that can be assigned to a slot instantly.

A ready stock video has already been rendered and Gemini-QC-passed, lives on the
`backup-stock` GitHub release, and has no slot until it is assigned. Health
checks every official IST slot (and any extra queue slot). If a live item fails
or QC-rejects, or an official slot is missing from the queue entirely, the next
unused stock video is assigned immediately. A queued item that is still
rendering is given until ~60 minutes before the slot. Buffer then posts it at
the slot time (or shareNow if late).
Used entries are never posted twice. Stock is only used when a slot has no fresh
video (Gemini out of quota / errored, or the run failed).
BIBI's rule (27 Nov): keep ~20 ready. When ready drops below 8 the manifest flips
to `refilling` and health dispatches a few stock renders per run (evergreen
sources-release topics only, 2 per run, at most 4 in flight so the Gemini keys
are not burned) until 20 are ready again. Ready <= 2 is an alert (BIBI's bot steps in).
Thresholds live in config/backup_stock.json (target / refill_at / alert_at).
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
TARGET = 20
REFILL_AT = 8
ALERT_AT = 7
MAX_REFILL = 2
MAX_IN_FLIGHT = 4
MIN_READY = REFILL_AT
RETIRE_AFTER = 2
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
            "alert_at": ALERT_AT,
            "max_in_flight": MAX_IN_FLIGHT,
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
    d.setdefault("alert_at", ALERT_AT)
    d.setdefault("max_in_flight", MAX_IN_FLIGHT)
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


def release_clip_count(b: dict, assets: set[str]) -> int:
    """How many of this pool topic's scene clips already exist on the sources release."""
    import sourcing
    names = {str(u or "").rstrip("/").rsplit("/", 1)[-1] for u in (b.get("fallback_mp4") or []) + (b.get("sources") or [])}
    names |= set(b.get("scene_assets") or [])
    have = {n for n in names if n in assets and not sourcing.is_trailer_source(n)}
    have |= set(sourcing.release_scene_assets(pool_slug(b), assets))
    return len(have)


def release_backed_pool(pool: list[dict]) -> list[dict]:
    """On GitHub (release-only) stock is filled only from topics whose clips are on the release.

    Topics with >=2 clips first (enough unique shots); 1-clip topics only if nothing better exists.
    Off GitHub / when the release cannot be listed the pool is returned unchanged.
    """
    try:
        import sourcing
    except Exception:
        return pool
    if not sourcing.release_only():
        return pool
    assets = sourcing.release_source_assets()
    if assets is None:
        return pool
    try:
        import title_swap
        assets = title_swap.usable_release_assets(assets, _repo())
        # A topic whose scene clips are all already posted (or inside other unposted
        # stock) can only produce a repeat: never refill from it.
        assets = title_swap.unposted_release_assets(assets, _repo(), reserved=True)
    except Exception:
        pass
    counted = [(release_clip_count(b, assets), b) for b in pool]
    # Never refill stock from footage known not to pass the no-logo/scene filter.
    try:
        import title_swap
        health = title_swap.release_clip_health(_repo())
        block = title_swap.release_title_blocklist(_repo())
        counted = [(n, b) for n, b in counted
                   if not title_swap.clips_known_bad(pool_slug(b), health, block)]
    except Exception:
        pass
    two = [b for n, b in counted if n >= 2]
    return two or [b for n, b in counted if n >= 1]


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


def must_not_publish(r: dict | None) -> bool:
    """True for demo / no_publish / test-renders results that must never go to Buffer."""
    if not r:
        return True
    if r.get("no_publish") or r.get("demo") or r.get("dry_run"):
        return True
    if r.get("footage_mismatch"):
        return True  # clips are from another show than the title/description (render_item guard)
    if "/test-renders/" in str(r.get("asset_url") or ""):
        return True
    iid = str(r.get("id") or "")
    if iid.startswith("demo-") or iid.startswith("test-"):
        return True
    return False


_DOWNLOAD_TOOLING = (
    "sign in to confirm", "not a bot", "failed to extract", "yt-dlp", "release-only",
    "no official scene clips", "no unused official source", "not on the sources release",
)


def _is_download_tooling(e: str) -> bool:
    e = (e or "").lower()
    return any(m in e for m in _DOWNLOAD_TOOLING)


def is_retire_worthy_error(err: str | None, r: dict | None = None) -> bool:
    """True when this failure means the topic's official footage is unusable.

    Logo / burned-in / 404 / YouTube bot-check counts. Tooling-only errors
    (yt-dlp fixed-output-name, ffmpeg install) and timeouts do not.
    """
    e = (err or "").lower()
    if _is_download_tooling(e) and not any(
            m in e for m in ("reject logo", "reject burned-in", "404")):
        return False  # the failure itself was a GitHub download problem, not the footage
    if r:
        e = (e + " " + str(r.get("detail") or "")).lower()
    source_markers = (
        "reject logo", "reject logo/watermark", "reject burned-in",
        "http error 404", "404: not found", "error 404",
    )
    has_source = any(m in e for m in source_markers)
    # GitHub-side download problems (YouTube bot wall, yt-dlp, clip not on the release yet) say
    # nothing about the footage: the box fetches those clips, so the topic is never retired for them.
    if not has_source and _is_download_tooling(e):
        return False
    tooling_only = (
        ("fixed output name" in e or "install ffmpeg" in e or "xz -t" in e)
        and not has_source
    )
    if tooling_only:
        return False
    if any(w in e for w in ("timed out", "timeout", "job cancelled", "no result json")) and not has_source:
        if not (r or {}).get("qc_rejected"):
            return False
    if has_source:
        return True
    if (r or {}).get("qc_rejected"):
        qc_markers = ("watermark", "logo", "burn-in", "burned-in", "subscribe")
        if any(m in e for m in qc_markers):
            return True
    return False


def retire_worthy_failures(root: Path | None = None) -> dict[str, list[dict]]:
    """Map topic slug -> stock/live results that failed on logo/404/bot-check."""
    root = root or _repo()
    by: dict[str, list[dict]] = {}
    for r in iter_results(root):
        if (r.get("status") or "") not in ("failed",):
            continue
        parsed = parse_stock_id(r.get("id") or "")
        slug = parsed[0] if parsed else _slug_of(r)
        if not slug:
            continue
        if not is_retire_worthy_error(r.get("last_error") or r.get("detail"), r):
            continue
        by.setdefault(slug, []).append({
            "id": r.get("id"),
            "reason": (r.get("last_error") or r.get("detail") or "source unusable")[:240],
        })
    return by


def retired_slugs(root: Path | None = None, man: dict | None = None) -> set[str]:
    man = man if man is not None else load_manifest(root)
    out: set[str] = set()
    for row in man.get("retired_topics") or []:
        if isinstance(row, str) and row.strip():
            out.add(row.strip().lower())
        elif isinstance(row, dict) and row.get("slug"):
            reason = str(row.get("reason") or "").lower()
            if _is_download_tooling(reason) and not any(
                    m in reason for m in ("reject logo", "reject burned-in", "404")):
                continue  # retired only for a GitHub download failure: usable again (row kept)
            out.add(str(row["slug"]).strip().lower())
    return out


def blocked_slugs(root: Path | None = None, man: dict | None = None) -> set[str]:
    """Topics that must not be refilled or newly queued (retired or already broken)."""
    man = man if man is not None else load_manifest(root)
    return retired_slugs(root, man) | set(retire_worthy_failures(root))


def _apply_retirements(man: dict, root: Path, now: datetime) -> list[dict]:
    """Stamp repeated logo/404 failures as retired topics. Never deletes videos."""
    already = retired_slugs(man=man)
    rows = list(man.get("retired_topics") or [])
    newly = []
    for slug, fails in retire_worthy_failures(root).items():
        if slug in already or len(fails) < RETIRE_AFTER:
            continue
        row = {
            "slug": slug,
            "reason": fails[-1].get("reason") or "repeated logo/404 source failures",
            "fail_count": len(fails),
            "failed_ids": [f["id"] for f in fails if f.get("id")][:20],
            "retired_at": now.strftime("%Y-%m-%d %H:%M IST"),
        }
        rows.append(row)
        newly.append(row)
        already.add(slug)
        print(f"retired topic {slug} after {len(fails)} unrecoverable source failures (videos kept)",
              file=sys.stderr)
    if newly:
        man["retired_topics"] = rows
    return newly


def retire_broken_topics(root: Path | None = None, now: datetime | None = None) -> list[dict]:
    root = root or _repo()
    now = now or now_ist()
    man = load_manifest(root)
    newly = _apply_retirements(man, root, now)
    if newly:
        save_manifest(man, root)
    return newly


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



def source_quality_reason(r: dict | None = None, path=None) -> str | None:
    """Why a stock source is below the 1080p floor, or None if ok/unknown.

    Never deletes the video; callers mark the entry not-ready and move on.
    """
    import sourcing
    if path:
        from pathlib import Path as _P
        fp = _P(path)
        if fp.exists():
            ok, why = sourcing.resolution_ok(fp)
            if not ok:
                return why
    if r:
        w, h = int(r.get("source_w") or r.get("width") or 0), int(r.get("source_h") or r.get("height") or 0)
        if sourcing.is_below_1080(w, h):
            return f"{w}x{h} below 1080p"
    return None


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
    if must_not_publish(r):
        return False
    if r.get("stock_ready") is False:
        return False
    if r.get("publish_blocked"):
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


def hook_key(t: str) -> str:
    """Title minus hashtags / ' | Show' suffix / emoji: same hook = same video to a viewer.

    'Why Hodaka cannot stop watching her #weatheringwithyou #hina #anime' and the
    '... #animeedit' variant are one video; Buffer's exact-title check missed that.
    """
    t = str(t or "").split("\n")[0]
    t = re.sub(r"#\w+", " ", t)
    t = t.split(" | ")[0]
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return " ".join(t.split())


def _is_posted(r: dict) -> bool:
    return bool(r.get("buffer_status") in ("sent", "scheduled") or r.get("youtube_url")
                or r.get("buffer_posted_at"))


def posted_hook_keys(root: Path, exclude_id: str | None = None) -> set[str]:
    """Hook keys of every result already sent/scheduled on Buffer (all time)."""
    keys = set()
    for r in iter_results(root):
        if r.get("id") == exclude_id or r.get("publish_blocked") or must_not_publish(r):
            continue
        if _is_posted(r):
            k = hook_key(r.get("title") or r.get("hook") or "")
            if k:
                keys.add(k)
    return keys


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
        if o.get("publish_blocked") or o.get("publish_dead") or o.get("buffer_status") == "dead":
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
        # A retry that never ran must time out too, or it blocks refill forever.
        at = str(r.get("at") or ent.get("queued_at") or "")
        try:
            dt = datetime.strptime(at.replace(" IST", "")[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            if now - dt >= timedelta(hours=PENDING_HOURS):
                return "failed"
        except Exception:
            pass
        return "pending"
    if ent.get("status") == "pending":
        at = ent.get("queued_at") or ""
        try:
            dt = datetime.strptime(at.replace(" IST", ""), "%Y-%m-%d %H:%M").replace(tzinfo=IST)
            if now - dt < timedelta(hours=PENDING_HOURS):
                return "pending"
            # Timed-out pending with no result must not stay "pending".
            # Otherwise ready+pending sits at the target and refill never runs.
            return "failed"
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
    _apply_retirements(man, root, now)
    mark_duplicate_scenes(man, root)
    update_refill_flag(man)
    save_manifest(man, root)
    return man


def _cfg(man: dict, key: str, default: int) -> int:
    try:
        return int(man.get(key) if man.get(key) is not None else default)
    except Exception:
        return default


def update_refill_flag(man: dict) -> bool:
    """Hysteresis: start refilling when ready < refill_at (8), stop once ready >= target (20)."""
    ready = len(ready_entries(man))
    if ready < _cfg(man, "refill_at", REFILL_AT):
        man["refilling"] = True
    elif ready >= _cfg(man, "target", TARGET):
        man["refilling"] = False
    else:
        man["refilling"] = bool(man.get("refilling"))
    return man["refilling"]


def refill_need(man: dict) -> int:
    """How many new stock renders may be queued now (0 = none)."""
    if not update_refill_flag(man):
        return 0
    ready = len(ready_entries(man))
    pending = len(pending_entries(man))
    need = _cfg(man, "target", TARGET) - ready - pending
    room = _cfg(man, "max_in_flight", MAX_IN_FLIGHT) - pending
    return max(0, min(need, room))


def mark_duplicate_scenes(man: dict, root: Path) -> list[str]:
    """Ready stock must be diverse and never a repeat.

    A ready entry is marked `dup_scene` (video kept, never assigned) when any of its scene
    clips / its mp4 is already posted, already sits in a slot waiting to post, or another
    earlier ready entry holds the same clip. At most one ready item per source scene."""
    import posted_history
    rows = iter_results(root)
    by_id = {r.get("id"): r for r in rows}
    hist = posted_history.index(root, rows)
    held = posted_history.reserved_clips(
        [r for r in rows if r.get("assigned_from_stock")])
    marked = []
    for e in man.get("items") or []:
        if e.get("status") != "ready":
            continue
        r = by_id.get(e.get("id")) or {}
        clips = set(posted_history.result_clips(r))
        why = posted_history.conflict(r, hist)
        if not why and clips & held:
            why = "same source scene already assigned to a slot: " + ", ".join(sorted(clips & held))
        if why:
            e["status"] = "dup_scene"
            e["dup_reason"] = why[:300]
            marked.append(e.get("id"))
            continue
        e.pop("dup_reason", None)
        held |= clips
    return marked


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
    status = {e.get("id"): e.get("status") for e in man.get("items") or []}
    for r in iter_results(root):
        if not parse_stock_id(r.get("id") or ""):
            continue
        if status.get(r.get("id")) not in (None, "ready"):
            continue  # dup_scene / used / failed per the manifest
        if is_ready_result(r) and not r.get("stock_used") and not r.get("assigned_slot"):
            return True
    return False


def pending_entries(man: dict) -> list[dict]:
    return [e for e in man.get("items") or [] if e.get("status") == "pending"]


def _recent_music_files(root: Path, now: datetime, days: float = 3) -> list[str]:
    """Songs of videos that were really used in the last `days` days (assigned/posted renders).

    Unassigned stock renders are not "used": counting them made every ready stock video block itself
    (its own song looked recently used), so no stock was ever assigned to a missed slot (27 Nov 2026).
    """
    import music_lib
    from datetime import timedelta
    cut = now - timedelta(days=days)
    out: list[str] = []
    for r in iter_results(root):
        if r.get("status") != "done" or not r.get("music"):
            continue
        if parse_stock_id(r.get("id") or "") and not r.get("stock_used") and not r.get("assigned_slot"):
            continue
        dt = music_lib._parse_result_when(r)
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        if dt >= cut:
            out.append(str(r["music"]))
    return list(dict.fromkeys(out))


def pick_stock(man: dict, root: Path, day: str, now: datetime | None = None,
               buffer_posts: list | None = None, slot_s: str | None = None) -> dict | None:
    ready = ready_entries(man)
    if not ready:
        return None
    now = now or now_ist()
    used = topics_used_on_day(root, day)
    import variety
    used_fr = {variety.franchise(s) for s in used}  # dandadanevil blocks dandadan2 the same day
    recent_titles, recent_slugs = recent_titles_and_topics(root, now, buffer_posts=buffer_posts)
    used |= recent_slugs
    posted = posted_hook_keys(root)
    recent_hooks = {hook_key(t) for t in recent_titles}
    import posted_history
    rows = iter_results(root)
    hist = posted_history.index(root, rows)
    held = posted_history.reserved_clips([r for r in rows if r.get("assigned_from_stock")])

    def conflicts(e: dict) -> bool:
        if (e.get("slug") or "") in used or variety.franchise(e.get("slug") or e.get("id") or "") in used_fr:
            return True
        src = read_result(root, e.get("id") or "") or {}
        # Never the same source scene / video that is already posted or waiting in a slot.
        if posted_history.conflict(src, hist) or set(posted_history.result_clips(src)) & held:
            return True
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
                recent = set(music_lib.expand_same_song(root, _recent_music_files(root, now)))
                if music in recent or any(music_lib._norm_music(music) == music_lib._norm_music(x) for x in recent):
                    return True
        except Exception:
            pass
        title = norm_title(src.get("title") or e.get("title") or "")
        if title and title in recent_titles:
            return True
        # Never re-post a hook that already went out (hashtag variants included).
        hk = hook_key(src.get("title") or e.get("title") or "")
        return bool(hk and (hk in posted or hk in recent_hooks))

    prefer = [e for e in ready if not conflicts(e)]
    # Variety: different category from the neighbour slots, franchise not within ~3 days.
    dt = variety.parse_slot(slot_s) if slot_s else None
    if dt and len(prefer) > 1:
        try:
            cat = variety.load_catalog(root)
            code = dt.astimezone(variety.IST).strftime("%Y%m%d-%H%M")
            ctx = [c for c in variety.context(root, dt, catalog=cat) if not c["id"].startswith(code)]
            prefer.sort(key=lambda e: variety.topic_penalty({"slug": e.get("slug") or "", "id": e.get("id") or ""},
                                                            dt, ctx, cat))
        except Exception:
            pass
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
    ent = pick_stock(man, root, day, now=now, slot_s=slot_s)
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
    # Refill only once ready < refill_at (8), then keep going (capped per run, few in
    # flight) until the target (20) is ready again.
    need = refill_need(man)
    if need <= 0 or cap <= 0:
        return []
    pool = [b for b in load_pool(root) if has_preuploaded(b)]
    pool = release_backed_pool(pool)
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
    blocked = blocked_slugs(root, man)
    picks = []
    # Rotate topics with the fewest in-stock copies first for variety.
    # Skip retired / repeatedly-broken slugs so they stop burning render slots.
    order = [b for b in sorted(pool, key=lambda b: (counts.get(pool_slug(b), 0), pool_slug(b)))
             if pool_slug(b) not in blocked]
    if not order:
        return []
    i = 0
    guard = 0
    while len(picks) < min(need, cap) and guard < 200:
        guard += 1
        b = order[i % len(order)]
        i += 1
        slug = pool_slug(b)
        if slug in blocked:
            continue
        # Each render merges all free clips of the topic: a second ready/pending copy
        # of the same topic would be the same source scenes again.
        if counts.get(slug, 0) >= 1:
            continue
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
        qreason = source_quality_reason(r)
        if qreason:
            reasons.append("below_1080p")
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
    retired = sorted(retired_slugs(root, man))
    refill_at = _cfg(man, "refill_at", REFILL_AT)
    alert_at = _cfg(man, "alert_at", ALERT_AT)
    below = len(ready) < refill_at
    need = refill_need(man)
    return {
        "ready": len(ready),
        "pending": len(pending),
        "used": len(used),
        "target": int(man.get("target") or TARGET),
        "min_ready": refill_at,
        "below_ready_floor": below,
        "refill_at": refill_at,
        "alert_at": alert_at,
        "refilling": bool(man.get("refilling")),
        "need_refill": need > 0,
        "alert": len(ready) <= alert_at,
        "ready_ids": [e["id"] for e in ready],
        "pending_ids": [e["id"] for e in pending],
        "retired": retired,
    }


def release_asset_names(tag: str = RELEASE) -> set[str] | None:
    """Asset names on the backup-stock release, or None if they cannot be listed (then nothing changes)."""
    import subprocess
    repo = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/skywalker-shorts")
    try:
        r = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/releases/tags/{tag}", "-q", ".assets[].name"],
                           capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return set(r.stdout.split())


def audit_lost_assets(root: Path | None = None, assets: set[str] | None = None,
                      now: datetime | None = None) -> list[str]:
    """A 'ready' stock video whose mp4 is no longer on the release (e.g. lost with the suspended account)
    must never be assigned to a slot. Stamps stock_ready=false + stock_lost; never deletes anything.
    Returns the ids it stamped. If the release cannot be listed, does nothing."""
    root = root or _repo()
    now = now or now_ist()
    if assets is None:
        assets = release_asset_names()
    if assets is None:
        return []
    stamped = []
    for r in iter_results(root):
        iid = r.get("id") or ""
        if not parse_stock_id(iid) or r.get("stock_used") or r.get("assigned_slot"):
            continue
        if r.get("status") != "done" or not r.get("asset_url"):
            continue
        name = str(r["asset_url"]).rsplit("/", 1)[-1]
        if name in assets or r.get("stock_lost"):
            continue
        r["stock_ready"] = False
        r["stock_lost"] = f"asset {name} not on the {RELEASE} release ({now:%Y-%m-%d %H:%M} IST)"
        write_result(root, r)
        stamped.append(iid)
    return stamped


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = (argv[0] if argv else "status").replace("_", "-")
    root = _repo()
    now = now_ist()
    if cmd == "assign":
        got = assign_needed(root, now)
        print(json.dumps({"assigned": [g["id"] for g in got], "n": len(got)}))
        return 0
    if cmd == "audit-lost":
        got = audit_lost_assets(root, now=now)
        if got:
            sync(root, now)
        print(json.dumps({"lost": got, "n": len(got)}))
        return 0
    if cmd == "retire":
        got = retire_broken_topics(root, now)
        print(json.dumps({"retired": [g["slug"] for g in got], "n": len(got),
                          "all": sorted(retired_slugs(root))}))
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
