"""Skip a failed title and continue with the next official title of the same format.

Used inside a single render job (including demo and forced-id items). A
romance/love edit only swaps to another official romance title; an action
edit only swaps to another action title. GTA 6 stays in its own format.
Trailers, sexual titles, and topics with no official scene clip are skipped.
The queue id, slot, and no_publish/demo flags stay on the original item.
"""
from __future__ import annotations

import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import edit_format
import sourcing

REPO = Path(__file__).resolve().parent
SOURCES_RELEASE = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources"
MAX_SWAPS = 4
RECENT_TITLE_DAYS = 3
IST = timezone(timedelta(hours=5, minutes=30))

# Keep identity / publish / render flags when the topic changes.
_KEEP = (
    "id", "slot", "demo", "no_publish", "dry_run", "status", "template",
    "target_dur", "chunked", "chunk_render", "market", "out_res", "stock",
    "stock_topic", "spare", "backup_item", "whisper_model", "dialogue_lang",
    "whisper_prompt",
)
_COPY = (
    "words", "hook", "title", "description", "tags", "source_url", "sources",
    "fallback_mp4", "fallback_asset", "fallback_release", "official_channels",
    "scene_assets", "content_kind", "pillar", "edit_format", "x_handles",
    "slug",
)

_SWAP_MARKERS = (
    "unique clean shots", "need ≥18", "need >=18", "need ≥20", "need >=20",
    "hard minimum", "only short unique", "under duration", "not publishing a short cut",
    "gemini qc reject", "qc reject", "leftover dialogue", "dialogue leftover",
    "reject logo", "reject burned-in", "logo/watermark", "logo flash",
    "third-party logo", "burned-in subtitle", "logo/text", "empty-dark/logo",
    "auto_qc", "failed empty-dark",
    "qa no_crop", "no_crop failed", "letterbox",
    "hook banner not visible", "white bar", "caption",
    "http error 404", "404: not found", "error 404",
    "sign in to confirm", "bot check", "not a bot", "confirm you’re not a bot",
    "confirm you're not a bot", "yt-dlp", "download failed",
    "no unused official source", "no official scene clips", "no usable official source",
    "no usable scenes",
    "under 1080", "below 1080", "resolution too low", "short side",
    "skip trailer",
    "no credited music", "music tracks found", "beat_sync", "publish gate failed",
    "pipeline/qa failed",
)

_TRANSIENT_JOB_DEATH = (
    "shutdown signal", "operation was canceled", "operation was cancelled",
    "timed out or cancelled", "no result json", "runner has received a shutdown",
)

_NO_SWAP = _TRANSIENT_JOB_DEATH + (
    "gemini qc skipped", "qc skipped", "qc still skipped",
)


def _listed_love_slugs(root: Path | None = None) -> set[str]:
    slugs: set[str] = set()
    for r in (edit_format.love_cfg(root).get("titles") or []):
        if not isinstance(r, dict):
            continue
        for k in ("slug", "words", "title"):
            tok = re.sub(r"[^a-z0-9]+", "", str(r.get(k) or "").lower())
            if len(tok) >= 4:
                slugs.add(tok)
    return slugs


def item_format(item: dict | None, root: Path | None = None) -> str:
    """love, gta6, or action.

    Strict on purpose: a hook that merely contains "loved" or "emotional"
    (Frieren) must not be treated as a romance title for auto-swap.
    """
    it = item or {}
    fmt = (it.get("edit_format") or "").strip().lower()
    if fmt in ("love", "romance"):
        return "love"
    if fmt == "gta6":
        return "gta6"
    if fmt == "action":
        return "action"
    if (it.get("pillar") or "").lower() in ("romance", "love"):
        return "love"
    kind = (it.get("content_kind") or "").lower()
    if any(k in kind for k in ("romance", "love scene", "romantic", "cute scene")):
        return "love"
    compact = re.sub(r"[^a-z0-9]+", "", " ".join([
        str(it.get("slug") or ""), str(it.get("words") or ""),
        str(it.get("id") or ""),
    ]).lower())
    if any(s and s in compact for s in _listed_love_slugs(root)):
        return "love"
    if edit_format.is_gta6_item(it):
        return "gta6"
    return "action"


def title_slug(item: dict | None) -> str:
    it = item or {}
    raw = (it.get("slug") or "").strip().lower()
    if raw:
        return re.sub(r"[^a-z0-9]+", "", raw)
    try:
        key = sourcing.topic_key(it)
        if key:
            return re.sub(r"[^a-z0-9]+", "", key)
    except Exception:
        pass
    words = re.sub(r"[^a-z0-9]+", "", (it.get("words") or "").lower())
    return words or re.sub(r"[^a-z0-9]+", "", (it.get("id") or "").lower())


def item_keys(item: dict | None) -> set[str]:
    keys = set()
    s = title_slug(item)
    if s:
        keys.add(s)
    try:
        keys |= {re.sub(r"[^a-z0-9]+", "", x) for x in sourcing.item_topic_slugs(item or {}) if x}
    except Exception:
        pass
    keys.discard("")
    return keys


def asset_url(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    if n.startswith("http://") or n.startswith("https://"):
        return n
    return f"{SOURCES_RELEASE}/{n.lstrip('/')}"


def failure_blob(reason) -> str:
    """Normalize an error string or result dict to a comparable lowercase blob."""
    if reason is None:
        return ""
    if isinstance(reason, dict):
        blob = " ".join(str(reason.get(k) or "") for k in ("error", "detail", "last_error"))
        qa = reason.get("qa")
        issues = reason.get("issues")
        if issues is None and isinstance(qa, dict):
            issues = qa.get("issues")
        if issues:
            blob += " " + " ".join(str(x) for x in (issues if isinstance(issues, list) else [issues]))
    else:
        blob = str(reason)
    return blob.lower().replace("≥", ">=").replace("≤", "<=")


def is_transient_job_death(reason) -> bool:
    """Runner shutdown / mid-encode cancel: retry the same title, do not swap."""
    e = failure_blob(reason)
    if not e.strip():
        return False
    if any(m in e for m in _SWAP_MARKERS):
        # A 404/bot-check in the same blob still counts as a source failure.
        return False
    return any(w in e for w in _TRANSIENT_JOB_DEATH)


def should_try_next_source(reason) -> bool:
    """True when this failure should advance to the next official clip of the same title.

    Job-death / QC-skipped cases stay on the current source so an outer retry
    or QC-only retry can finish it.
    """
    e = failure_blob(reason)
    if not e.strip():
        return False
    if any(w in e for w in _NO_SWAP) and not any(m in e for m in _SWAP_MARKERS):
        return False
    return True


def should_swap(reason) -> bool:
    """True for a title/clip failure that another same-format title can replace.

    Transient timeouts, runner shutdowns, and mid-encode cancels stay on the
    same title so the chunked path can retry it.
    """
    e = failure_blob(reason)
    if not e.strip():
        return False
    if is_transient_job_death(reason):
        return False
    if any(w in e for w in _NO_SWAP) and not any(m in e for m in _SWAP_MARKERS):
        return False
    if sourcing.is_unrecoverable_source_error(e):
        return True
    if any(m in e for m in _SWAP_MARKERS):
        return True
    # Any other real pipeline/QA/download failure also swaps once this title
    # is out of official clips — do not hard-fail while candidates remain.
    return should_try_next_source(reason)


# recovery_action() return values
NEXT_SOURCE = "next_source"
NEXT_TITLE = "next_title"
BACKUP = "backup"
RETRY_SAME = "retry_same"
GIVE_UP = "give_up"


def recovery_action(reason, *, sources_left: bool, titles_left: bool,
                    backups_left: bool) -> str:
    """Fall-through policy: next clip → next same-format title → backup → stop.

    Never returns give_up while a candidate remains, except for mid-job
    runner death (retry_same) which must not start a different title.
    """
    if is_transient_job_death(reason):
        return RETRY_SAME
    e = failure_blob(reason)
    if not e.strip():
        return GIVE_UP
    if any(w in e for w in _NO_SWAP) and not any(m in e for m in _SWAP_MARKERS):
        return RETRY_SAME
    if sources_left and should_try_next_source(reason):
        return NEXT_SOURCE
    if titles_left and should_swap(reason):
        return NEXT_TITLE
    if backups_left:
        return BACKUP
    return GIVE_UP


def _collect_urls(raw: dict) -> list[str]:
    urls: list[str] = []

    def add(u: str) -> None:
        u = (u or "").strip()
        if not u:
            return
        if not u.startswith("http") and not u.lower().endswith(".mp4"):
            # bare asset filename
            u = asset_url(u)
        elif not u.startswith("http") and u.lower().endswith(".mp4"):
            u = asset_url(u)
        if u not in urls:
            urls.append(u)

    for key in ("sources", "fallback_mp4", "scene_urls", "alt_source_urls", "youtube_urls"):
        for u in raw.get(key) or []:
            add(str(u))
    for a in raw.get("scene_assets") or []:
        add(str(a))
    fa = raw.get("fallback_asset") or ""
    if fa:
        add(str(fa))
    return sourcing.scene_only(urls)


def _normalize(raw: dict, slug: str = "", root: Path | None = None) -> dict | None:
    it = {k: v for k, v in (raw or {}).items() if v is not None}
    slug = (slug or it.get("slug") or title_slug(it) or "").strip()
    if slug and not it.get("slug"):
        it["slug"] = slug
    if slug and not it.get("id"):
        it["id"] = slug
    urls = _collect_urls(it)
    # Merge official scene URLs from the source catalog when we have a slug.
    if slug:
        try:
            cat = _source_topics(root)
            extra = cat.get(slug) or cat.get(re.sub(r"[^a-z0-9]+", "", slug)) or {}
            for u in (extra.get("scene_urls") or []) + (extra.get("youtube_urls") or []):
                if u and u not in urls and not sourcing.is_trailer_source(u):
                    urls.append(u)
            if extra.get("official_channels") and not it.get("official_channels"):
                it["official_channels"] = list(extra["official_channels"])
        except Exception:
            pass
    urls = sourcing.scene_only(urls)
    if not urls:
        return None
    it["sources"] = urls
    it["fallback_mp4"] = list(urls)
    if not it.get("fallback_asset"):
        name = ""
        for u in urls:
            cand = sourcing.source_filename(u)
            if cand and sourcing.source_topic_slug(cand or u):
                name = cand
                break
        if name:
            it["fallback_asset"] = name
            it.setdefault("fallback_release", "sources")
    elif sourcing.is_trailer_source(it.get("fallback_asset") or ""):
        # Prefer a real scene asset as the named fallback.
        for u in urls:
            n = sourcing.source_filename(u)
            if n and not sourcing.is_trailer_source(n):
                it["fallback_asset"] = n
                it.setdefault("fallback_release", "sources")
                break
    if not it.get("words") and slug:
        it["words"] = slug.replace("-", " ").upper()
    fmt = item_format(it, root=root)
    if fmt == "love":
        it["edit_format"] = "love"
        it["pillar"] = "romance"
        it.setdefault("content_kind", "romance scene")
    elif fmt == "gta6":
        it["edit_format"] = "gta6"
    if edit_format.is_explicit_item(it):
        return None
    kind = (it.get("content_kind") or "").lower()
    if "trailer" in kind and not any("scene" in sourcing.source_filename(u).lower() for u in urls):
        return None
    if not sourcing.item_has_scene_source(it):
        return None
    return it


def _source_topics(root: Path | None = None) -> dict:
    path = (Path(root) if root else REPO) / "config" / "source_catalog.json"
    try:
        return json.loads(path.read_text()).get("topics") or {}
    except Exception:
        return {}


def _load_pool(root: Path | None = None) -> list[dict]:
    root = Path(root) if root else REPO
    found: list[dict] = []
    seen: set[str] = set()

    def consider(raw: dict, slug: str = "") -> None:
        it = _normalize(raw, slug=slug, root=root)
        if not it:
            return
        key = title_slug(it)
        if not key or key in seen:
            return
        seen.add(key)
        found.append(it)

    try:
        for raw in json.loads((root / "config" / "topic_catalog.json").read_text()).get("items") or []:
            consider(raw, slug=str(raw.get("slug") or ""))
    except Exception:
        pass
    try:
        for raw in (edit_format.love_cfg(root).get("titles") or []):
            if isinstance(raw, dict):
                consider(dict(raw), slug=str(raw.get("slug") or ""))
    except Exception:
        pass
    try:
        for raw in json.loads((root / "config" / "backup_pool.json").read_text()).get("items") or []:
            bid = str(raw.get("id") or "")
            slug = bid[len("backup-"):] if bid.startswith("backup-") else bid
            consider(raw, slug=slug)
    except Exception:
        pass
    # Queue items (other official titles already prepared for a slot or demo).
    qdir = root / "queue"
    if qdir.is_dir():
        for p in sorted(qdir.glob("*.json")):
            try:
                rows = json.loads(p.read_text())
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                if raw.get("retired") or (raw.get("status") or "") in ("retired", "cancelled"):
                    continue
                consider(raw, slug=str(raw.get("slug") or title_slug(raw)))
    return found


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _parse_when(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    # "2026-11-18 02:39 IST"
    if raw.endswith(" IST"):
        raw = raw[:-4].strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        return None


def _in_recent_window(dt: datetime | None, *, days: int, now: datetime) -> bool:
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    start = (now - timedelta(days=days)).astimezone(IST).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Look ahead far enough to cover the filled official queue (min 4 days + buffer).
    end = (now + timedelta(days=6)).astimezone(IST).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    return start <= dt.astimezone(IST) <= end


def _slug_from_id(iid: str) -> str:
    parts = (iid or "").split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return "-".join(parts[2:])
    if (iid or "").startswith("backup-"):
        return iid[len("backup-"):]
    if (iid or "").startswith("stock-"):
        bits = iid.split("-")
        if len(bits) >= 2:
            return bits[1] if not bits[-1].isdigit() else "-".join(bits[1:-1])
    return ""


def recently_used_slugs(root: Path | None = None, *, days: int = RECENT_TITLE_DAYS,
                        now: datetime | None = None) -> set[str]:
    """Slugs/words posted or already queued in the last `days` plus the next 2 days.

    Includes auto-swap destinations (swapped_to / words) so a Spy x Family slot
    that published as Animal still blocks Animal from being reused.
    """
    root = Path(root) if root else REPO
    now = now or datetime.now(IST)
    keys: set[str] = set()

    def add(*vals: object) -> None:
        for v in vals:
            if not v:
                continue
            raw = str(v).strip()
            if raw.lower().startswith("backup-"):
                raw = raw[len("backup-"):]
            if raw.lower().startswith("stock-"):
                raw = _slug_from_id("stock-" + raw[len("stock-"):]) or raw
            k = _norm_key(raw)
            if len(k) >= 4:
                keys.add(k)

    for p in (root / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("no_publish") or r.get("demo"):
            continue
        posted = (
            r.get("buffer_status") in ("sent", "scheduled")
            or r.get("buffer_post_id") or r.get("youtube_url")
            or r.get("assigned_from_stock") or r.get("assigned_slot")
        )
        if not posted:
            continue
        when = None
        for key in ("buffer_posted_at", "buffer_due_at", "slot", "assigned_slot",
                    "used_at", "at", "buffer_created_at"):
            when = _parse_when(str(r.get(key) or ""))
            if when:
                break
        if when is None:
            # 20261118-0630-spyxfamily3 or stock-animal-01 dates from filename prefix
            m = re.match(r"(20\d{6})", p.stem)
            if m:
                try:
                    when = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=IST)
                except ValueError:
                    when = None
        if when and not _in_recent_window(when, days=days, now=now):
            continue
        if when is None:
            continue
        # After an auto-swap, the original queue id (e.g. spyxfamily3) was not
        # posted — only the destination title was. Do not block the original.
        swapped_to = r.get("swapped_to") or ""
        hist = [h for h in (r.get("title_swaps") or []) if isinstance(h, dict)]
        if not swapped_to and hist:
            swapped_to = hist[-1].get("to") or ""
        if swapped_to:
            add(swapped_to, r.get("words"), r.get("slug"))
            for h in hist:
                add(h.get("to"), h.get("to_words"))
        else:
            add(
                r.get("slug"), r.get("words"),
                _slug_from_id(str(r.get("id") or "")),
                r.get("stock_topic"), r.get("backup_item"),
            )

    for p in (root / "queue").glob("2026-*.json"):
        try:
            items = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("status") in ("replaced", "retired", "cancelled"):
                continue
            when = _parse_when(str(it.get("slot") or ""))
            if when is None:
                try:
                    when = datetime.strptime(p.stem, "%Y-%m-%d").replace(tzinfo=IST)
                except ValueError:
                    when = None
            if not _in_recent_window(when, days=days, now=now):
                continue
            # Past slots are covered by results (the posted title, not the
            # original queue id after an auto-swap). Only block still-planned items.
            if when and when < now:
                continue
            add(
                it.get("slug"), it.get("words"), it.get("swapped_to"),
                _slug_from_id(str(it.get("id") or "")),
            )

    return keys


def release_clip_names(cand: dict, assets: set[str] | None) -> list[str]:
    """Scene assets of this candidate that really exist on the sources release."""
    if not assets:
        return []
    names = set()
    for u in (cand.get("sources") or []) + (cand.get("fallback_mp4") or []):
        n = str(u or "").rstrip("/").rsplit("/", 1)[-1]
        if n in assets and not sourcing.is_trailer_source(n):
            names.add(n)
    names |= set(sourcing.release_scene_assets(title_slug(cand), assets))
    return sorted(names)


def with_release_clips(cand: dict, assets: set[str] | None) -> dict | None:
    """Release-only mode: point the candidate at its existing release clips; None if it has none."""
    names = release_clip_names(cand, assets)
    if not names:
        return None
    out = dict(cand)
    urls = [asset_url(n) for n in names]
    out["sources"] = urls
    out["fallback_mp4"] = list(urls)
    out["fallback_release"] = "sources"
    out["fallback_asset"] = names[0]
    out["scene_assets"] = list(names)
    return out


CLIP_HEALTH_DAYS = 7
# Failures that mean the title's release clips themselves do not pass the scene filter.
_CLIP_FILTER_FAIL = ("no usable scenes", "unique clean shots", "logo", "watermark", "burned")


def release_title_blocklist(root: Path | None = None) -> dict:
    root = Path(root) if root else REPO
    try:
        return dict(json.loads((root / "config" / "release_title_blocklist.json").read_text()).get("titles") or {})
    except Exception:
        return {}


def _result_footage_slug(r: dict) -> str:
    """Slug of the footage a result actually rendered (after any swap)."""
    s = r.get("swapped_to") or _slug_from_id(str(r.get("id") or ""))
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def release_clip_health(root: Path | None = None, *, days: int = CLIP_HEALTH_DAYS,
                        now: datetime | None = None) -> dict[str, dict]:
    """{slug: {"ok": n, "bad": n}} from recent results: did this title's clips render?

    ok  = a done render from this title's footage.
    bad = a render that died because its clips did not pass the scene/logo filter.
    """
    root = Path(root) if root else REPO
    now = now or datetime.now(IST)
    start = now - timedelta(days=days)
    out: dict[str, dict] = {}
    for p in (root / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("demo") or str(r.get("id") or "").startswith(("test-", "demo-")):
            continue
        at = str(r.get("at") or "")
        try:
            when = datetime.strptime(at[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except Exception:
            when = datetime.fromtimestamp(p.stat().st_mtime, IST)
        if when < start:
            continue
        slug = _result_footage_slug(r)
        if not slug:
            continue
        h = out.setdefault(slug, {"ok": 0, "bad": 0})
        st = r.get("status")
        if st == "done" and r.get("asset_url"):
            h["ok"] += 1
        elif st in ("failed", "retry_queued"):
            blob = " ".join(str(r.get(k) or "") for k in ("detail", "last_error")).lower()
            if any(m in blob for m in _CLIP_FILTER_FAIL):
                h["bad"] += 1
    return out


# Per-asset failures: the clip itself did not pass the scene/logo/crop filter (a title can have
# one clean clip and one Netflix-logo clip, so title-level health alone keeps retrying the dirty one).
_ASSET_FILTER_FAIL = _CLIP_FILTER_FAIL + ("under duration", "no_crop", "near-duplicate")
ASSET_BAD_MIN = 2


def _asset_name(src) -> str:
    u = str(src or "")
    if "/releases/download/sources/" not in u:
        return ""
    return u.rstrip("/").rsplit("/", 1)[-1]


def release_asset_blocklist(root: Path | None = None) -> dict:
    root = Path(root) if root else REPO
    try:
        return dict(json.loads((root / "config" / "release_asset_blocklist.json").read_text()).get("assets") or {})
    except Exception:
        return {}


def release_asset_health(root: Path | None = None, *, days: int = CLIP_HEALTH_DAYS,
                         now: datetime | None = None) -> dict[str, dict]:
    """{asset: {"ok": n, "bad": n}} from recent results' per-source attempts."""
    root = Path(root) if root else REPO
    now = now or datetime.now(IST)
    start = now - timedelta(days=days)
    out: dict[str, dict] = {}
    for p in (root / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        at = str(r.get("at") or "")
        try:
            when = datetime.strptime(at[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except Exception:
            when = datetime.fromtimestamp(p.stat().st_mtime, IST)
        if when < start:
            continue
        per_src = [a for a in r.get("source_attempts") or [] if isinstance(a, dict)]
        logged = {(str(a.get("source")), str(a.get("error") or "")[:120]) for a in per_src}
        rows = per_src + [a for a in r.get("attempts") or [] if isinstance(a, dict)
                          and (a.get("ok") or (str(a.get("source")), str(a.get("error") or "")[:120]) not in logged)]
        for a in rows:
            name = _asset_name(a.get("source"))
            if not name:
                continue
            h = out.setdefault(name, {"ok": 0, "bad": 0})
            if a.get("ok"):
                h["ok"] += 1
            elif any(m in str(a.get("error") or "").lower() for m in _ASSET_FILTER_FAIL):
                h["bad"] += 1
    return out


def bad_release_assets(root: Path | None = None, *, health: dict | None = None,
                       blocklist: dict | None = None) -> set[str]:
    """Release clips not worth another 15-20 min render attempt.

    >=ASSET_BAD_MIN recent filter failures (logo / burned-in text / too few clean shots /
    crop / near-duplicate) and at least 3 failures per success, or the title is blocklisted.
    """
    health = release_asset_health(root) if health is None else health
    block = release_title_blocklist(root) if blocklist is None else blocklist
    bad = set(release_asset_blocklist(root))
    bad |= {n for n, h in health.items() if h.get("bad", 0) >= ASSET_BAD_MIN
           and h.get("bad", 0) >= 3 * h.get("ok", 0)}
    for slug in block:
        bad |= {n for n in health if n.startswith(f"{slug}_scene")}
    return bad


def usable_release_assets(assets: set[str] | None, root: Path | None = None) -> set[str] | None:
    """Release asset names minus quarantined (blocklisted / repeatedly failing) clips."""
    if assets is None:
        return None
    try:
        bad = bad_release_assets(root)
    except Exception:
        return set(assets)
    block = release_title_blocklist(root)
    return {a for a in assets if a not in bad and not any(a.startswith(f"{s}_scene") for s in block)}


def unposted_release_assets(assets: set[str] | None, root: Path | None = None, *,
                            reserved: bool = False) -> set[str] | None:
    """Release clips never posted on the channel (posted_history), optionally minus clips
    held by ready / assigned stock. A clip that is already in a posted Short is never reused."""
    if assets is None:
        return None
    try:
        import posted_history
        gone = posted_history.blocked_clips(root, reserved=reserved)
    except Exception:
        return set(assets)
    return {a for a in assets if posted_history.clip_name(a) not in gone}


def clips_known_bad(slug: str, health: dict, blocklist: dict) -> bool:
    """Blocklisted, or >=2 recent filter failures and never a successful render."""
    k = re.sub(r"[^a-z0-9]+", "", (slug or "").lower())
    if k in blocklist:
        return True
    h = health.get(k) or {}
    return h.get("bad", 0) >= 2 and h.get("ok", 0) == 0


def _clip_rank(c: dict, health: dict) -> tuple:
    """Proven titles (a recent done render) first, then most release clips."""
    h = health.get(title_slug(c)) or {}
    return (0 if h.get("ok", 0) > 0 else 1, -len(c.get("scene_assets") or []))


def _release_filter(cands: list[dict], root: Path | None = None) -> list[dict]:
    """On GitHub (release-only) keep only titles whose clips are on the release, most clips first."""
    if not sourcing.release_only():
        return cands
    assets = usable_release_assets(sourcing.release_source_assets(), root)
    if assets is None:
        return cands
    # Never swap to footage that is already on the channel (same source scene = repeat).
    assets = unposted_release_assets(assets, root)
    out = [c2 for c2 in (with_release_clips(c, assets) for c in cands) if c2]
    # A fallback must be footage that actually passes the filter: drop titles whose
    # release clips are logo-burned (blocklist) or keep failing the scene filter.
    health = release_clip_health(root)
    block = release_title_blocklist(root)
    out = [c for c in out if not clips_known_bad(title_slug(c), health, block)]
    out.sort(key=lambda c: _clip_rank(c, health))
    return out


def same_format_candidates(item: dict, *, exclude: set[str] | None = None,
                           root: Path | None = None) -> list[dict]:
    fmt = item_format(item, root=root)
    blocked = set(exclude or []) | item_keys(item)
    blocked |= {re.sub(r"[^a-z0-9]+", "", x) for x in blocked}
    blocked |= recently_used_slugs(root)
    out = []
    for cand in _load_pool(root):
        if item_format(cand, root=root) != fmt:
            continue
        keys = item_keys(cand)
        if keys & blocked:
            continue
        out.append(cand)
    if sourcing.release_only():
        # Only titles with >=2 clips on the release; else next_fallback_item picks (recent allowed).
        return [c for c in _release_filter(out, root) if len(c.get("scene_assets") or []) >= 2]
    # Prefer titles that already have official sources-release scene clips.
    def score(c: dict) -> tuple:
        urls = c.get("sources") or []
        release = sum(1 for u in urls if "releases/download/sources" in (u or "") or (u or "").endswith(".mp4"))
        return (release, len(urls))
    out.sort(key=score, reverse=True)
    vkey = variety_ranker(item, root)
    out.sort(key=vkey)  # stable: variety first, then release clips
    return out


def proven_demo_title(item: dict, *, exclude: set[str] | None = None,
                      root: Path | None = None) -> dict | None:
    """Demo swap target: footage with a recent DONE render (proven clean shots), same format first.

    Demos never post, so a recently used title is fine. A random same-format title is not:
    demo-v14/v15 swapped Superman (trailer-only) -> Lazarus (Adult Swim bug) / KGF (no clips)
    -> Bleach (14.9-17.2s of clean shots) and never produced a video."""
    blocked = set(exclude or []) | item_keys(item)
    blocked |= {re.sub(r"[^a-z0-9]+", "", x) for x in blocked}
    cands = [c for c in _load_pool(root) if not (item_keys(c) & blocked)]
    cands = _release_filter(cands, root)
    health = release_clip_health(root)
    proven = [c for c in cands if (health.get(title_slug(c)) or {}).get("ok", 0) > 0]
    fmt = item_format(item, root=root)
    proven.sort(key=lambda c: (item_format(c, root=root) != fmt,
                               -(health.get(title_slug(c)) or {}).get("ok", 0),
                               (health.get(title_slug(c)) or {}).get("bad", 0),
                               -len(c.get("scene_assets") or [])))
    return proven[0] if proven else None


def next_eligible_title(item: dict, *, exclude: set[str] | None = None,
                        root: Path | None = None) -> dict | None:
    if item.get("demo"):
        p = proven_demo_title(item, exclude=exclude, root=root)
        if p:
            return p
    cands = same_format_candidates(item, exclude=exclude, root=root)
    if cands and sourcing.release_only():
        return _spread_pick(cands, item, root)
    return cands[0] if cands else None


def next_fallback_item(item: dict, *, exclude: set[str] | None = None,
                       root: Path | None = None, same_format_only: bool = False) -> dict | None:
    """Same-format title first, then any other official pool/backup title."""
    nxt = next_eligible_title(item, exclude=exclude, root=root)
    if nxt or same_format_only:
        return nxt
    blocked = set(exclude or []) | item_keys(item)
    blocked |= {re.sub(r"[^a-z0-9]+", "", x) for x in blocked}
    rest = [c for c in _load_pool(root) if not (item_keys(c) & blocked)]
    if sourcing.release_only():
        # Never leave a slot empty: any title with clips already on the release (recent or not).
        recent = recently_used_slugs(root)
        rest = _release_filter(rest, root)
        health = release_clip_health(root)
        # Proven footage first (a recent done render), then >=2 clips (a 1-clip title
        # rarely has 18s of unique clean shots), then not-recent, then most clips.
        return _spread_pick(rest, item, root, health=health, recent=recent)
    if rest:
        rest.sort(key=variety_ranker(item, root))
    return rest[0] if rest else None


def _spread_pick(cands: list[dict], item: dict, root: Path | None, *, health: dict | None = None,
                 recent: set[str] | None = None) -> dict | None:
    """Best release-backed candidate; parallel slot renders are spread over the top tier.

    Order: proven footage (a recent done render) > not used today > >=2 clips > not recent.
    """
    if not cands:
        return None
    health = release_clip_health(root) if health is None else health
    recent = set(recent or ())
    today = used_today_slugs(item, root)
    vkey = variety_ranker(item, root)

    def tier_key(c: dict) -> tuple:
        v = vkey(c)
        # Variety first: a franchise already on this IST day is the last resort (BiBi 25 Nov).
        return (v[0], _clip_rank(c, health)[0], bool(item_keys(c) & today), len(c.get("scene_assets") or []) < 2, v[1])

    ordered = sorted(cands, key=lambda c: tier_key(c) + (bool(item_keys(c) & recent),
                                                         -len(c.get("scene_assets") or [])))
    k0 = tier_key(ordered[0])
    tier = [c for c in ordered if tier_key(c) == k0]
    return tier[_slot_spread_index(item) % len(tier)]


def variety_ranker(item: dict, root: Path | None = None):
    """c -> (same franchise already on this day?, variety penalty bucket) for this item's slot."""
    try:
        import variety
        root = Path(root) if root else REPO
        dt = variety.slot_of(item)
        if dt is None:
            return lambda c: (False, 0)
        cat = variety.load_catalog(root)
        ctx = variety.context(root, dt, exclude_ids={str(item.get("id") or "")}, catalog=cat)
        seen_ids = {c["id"] for c in ctx}
        for r in (root / "results").glob(f"{dt.astimezone(IST):%Y%m%d}-*.json"):
            if r.stem == str(item.get("id") or "") or r.stem in seen_ids:
                continue
            try:
                rr = json.loads(r.read_text())
            except Exception:
                continue
            if rr.get("status") in ("done", "rendering") or rr.get("assigned_from_stock"):
                ctx.append({"id": r.stem, "dt": variety.slot_of(rr) or dt,
                            "franchise": variety.franchise(_result_footage_slug(rr)),
                            "category": variety.category({"slug": _result_footage_slug(rr)}, cat)})

        def key(c: dict) -> tuple:
            p = variety.topic_penalty({**c, "slug": title_slug(c)}, dt, ctx, cat)
            return (p == variety.FORBIDDEN, 0 if p == variety.FORBIDDEN else int(p // 10))
        return key
    except Exception:
        return lambda c: (False, 0)


def _slot_spread_index(item: dict) -> int:
    """0,1,2.. for the day's official slots (06:30 -> 0); stable hash for anything else."""
    iid = str(item.get("id") or "")
    parts = iid.split("-")
    try:
        import slot_times
        times = [t.replace(":", "") for t in slot_times.official_times()]
        if len(parts) >= 2 and parts[1] in times:
            return times.index(parts[1])
    except Exception:
        pass
    return zlib.crc32(iid.encode())


def used_today_slugs(item: dict, root: Path | None = None) -> set[str]:
    """Footage slugs other results of this IST day already rendered/assigned."""
    root = Path(root) if root else REPO
    iid = str(item.get("id") or "")
    day = iid[:8] if iid[:8].isdigit() else datetime.now(IST).strftime("%Y%m%d")
    out: set[str] = set()
    for p in (root / "results").glob(f"{day}-*.json"):
        if p.stem == iid:
            continue
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("status") in ("done", "rendering", "queued") or r.get("assigned_from_stock"):
            s = _result_footage_slug(r)
            if s:
                out.add(s)
    return out


def apply_title_swap(item: dict, candidate: dict, *, reason: str = "") -> dict:
    """Rewrite sources/hook/words onto item. Keep id, slot, demo, no_publish."""
    before = {
        "slug": title_slug(item),
        "words": item.get("words"),
        "hook": item.get("hook"),
    }
    for k in _COPY:
        item.pop(k, None)
    item.pop("alt_source_urls", None)
    item.pop("source", None)
    for k in _COPY:
        if candidate.get(k) is not None:
            item[k] = candidate[k]
    # Always take the candidate's official scene list.
    urls = list(candidate.get("sources") or candidate.get("fallback_mp4") or [])
    item["sources"] = list(urls)
    item["fallback_mp4"] = list(urls)
    fmt = item_format(item)
    if fmt == "love":
        item["edit_format"] = "love"
        item["pillar"] = "romance"
        item["content_kind"] = item.get("content_kind") or "romance scene"
        item["edit_mode"] = "music_only"  # no dialogue anywhere
    elif fmt == "gta6":
        item["edit_format"] = "gta6"
        item["edit_mode"] = "music_only"
    else:
        item["edit_format"] = item.get("edit_format") or "action"
        item["edit_mode"] = "music_only"
    hist = list(item.get("title_swaps") or [])
    hist.append({
        "from": before["slug"],
        "from_words": before["words"],
        "to": title_slug(item),
        "to_words": item.get("words"),
        "reason": (reason or "")[:400],
    })
    item["title_swaps"] = hist
    item["swapped_from"] = before["slug"]
    item["swapped_to"] = title_slug(item)
    return item


# ---------------------------------------------------------------------------
# Footage <-> metadata guard. The 20261121-1330-yourname QC-only retry judged a
# Jujutsu Kaisen render (backup fallthrough in the previous run) against the
# untouched "Your Name" queue title, so Gemini QC rejected it. Metadata must
# always describe the show whose clips are actually in the video.

_SWAP_META_KEYS = ("words", "hook", "title", "description", "tags", "edit_format",
                   "title_swaps", "swapped_from", "swapped_to")


def _clip_ref(source: str | None) -> str:
    s = str(source or "").strip()
    if s.startswith("gh:"):
        s = s[3:]
    return s


def restore_swap_from_result(item: dict, prev: dict | None) -> bool:
    """Re-apply a title/backup swap recorded in an earlier result onto the queue item.

    A QC-only retry reloads the ORIGINAL queue item, while the kept render may be a
    different show (swapped mid-run). Copy that show's title/description/hashtags back
    and pin the item's sources to the clip actually rendered. Returns True if changed."""
    prev = prev or {}
    to = re.sub(r"[^a-z0-9]+", "", str(prev.get("swapped_to") or "").lower())
    if not to or to == title_slug(item):
        return False
    for k in _SWAP_META_KEYS:
        if prev.get(k) is not None:
            item[k] = prev[k]
    item["slug"] = to
    clip = _clip_ref(prev.get("source"))
    if clip:
        item["sources"] = [clip]
        item["fallback_mp4"] = [clip]
        item.pop("fallback_asset", None)
        item.pop("fallback_release", None)
        item.pop("alt_source_urls", None)
    return True


def footage_title_mismatch(item: dict | None, source: str | None) -> str:
    """'' when the rendered clip belongs to the show the metadata describes, else a reason.

    Only sources-release asset names carry a show slug; page URLs (YouTube/X) return ''."""
    clip = _clip_ref(source)
    slug = sourcing.source_topic_slug(clip)
    if not clip or not slug:
        return ""
    if sourcing.source_matches_topic(item or {}, clip):
        return ""
    return (f"footage/title mismatch: clips are from '{slug}' but title/description/hashtags "
            f"are for '{title_slug(item) or (item or {}).get('words') or '?'}'")
