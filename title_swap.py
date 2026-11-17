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
from pathlib import Path

import edit_format
import sourcing

REPO = Path(__file__).resolve().parent
SOURCES_RELEASE = "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources"
MAX_SWAPS = 4

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
    "hard minimum", "only short unique", "under duration",
    "gemini qc reject", "qc reject",
    "reject logo", "reject burned-in", "logo/watermark", "logo flash",
    "third-party logo", "burned-in subtitle",
    "http error 404", "404: not found", "error 404",
    "sign in to confirm", "bot check", "not a bot", "confirm you’re not a bot",
    "confirm you're not a bot",
    "no unused official source", "no official scene clips", "no usable official source",
    "under 1080", "below 1080", "resolution too low",
    "skip trailer",
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


def should_swap(reason) -> bool:
    """True for a title/clip failure that another same-format title can replace.

    Transient timeouts, runner shutdowns, and mid-encode cancels stay on the
    same title so the chunked path can retry it.
    """
    if reason is None:
        return False
    if isinstance(reason, dict):
        blob = " ".join(str(reason.get(k) or "") for k in ("error", "detail", "last_error"))
        issues = reason.get("issues") or reason.get("qa", {}).get("issues") if isinstance(reason.get("qa"), dict) else reason.get("issues")
        if issues:
            blob += " " + " ".join(str(x) for x in (issues if isinstance(issues, list) else [issues]))
    else:
        blob = str(reason)
    e = blob.lower().replace("≥", ">=").replace("≤", "<=")
    if not e.strip():
        return False
    transient = (
        "shutdown signal", "operation was canceled", "operation was cancelled",
        "timed out or cancelled", "no result json", "runner has received a shutdown",
        "temporarily", "connection reset", "connection aborted",
        "network is unreachable",
    )
    # A 404/bot-check beats a generic "timeout" word in the same blob.
    if any(w in e for w in transient) and not any(m in e for m in _SWAP_MARKERS):
        return False
    if sourcing.is_unrecoverable_source_error(e):
        return True
    return any(m in e for m in _SWAP_MARKERS)


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
        name = sourcing.source_filename(urls[0])
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


def same_format_candidates(item: dict, *, exclude: set[str] | None = None,
                           root: Path | None = None) -> list[dict]:
    fmt = item_format(item, root=root)
    blocked = set(exclude or []) | item_keys(item)
    blocked |= {re.sub(r"[^a-z0-9]+", "", x) for x in blocked}
    out = []
    for cand in _load_pool(root):
        if item_format(cand, root=root) != fmt:
            continue
        keys = item_keys(cand)
        if keys & blocked:
            continue
        out.append(cand)
    # Prefer titles that already have official sources-release scene clips.
    def score(c: dict) -> tuple:
        urls = c.get("sources") or []
        release = sum(1 for u in urls if "releases/download/sources" in (u or "") or (u or "").endswith(".mp4"))
        return (release, len(urls))
    out.sort(key=score, reverse=True)
    return out


def next_eligible_title(item: dict, *, exclude: set[str] | None = None,
                        root: Path | None = None) -> dict | None:
    cands = same_format_candidates(item, exclude=exclude, root=root)
    return cands[0] if cands else None


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
        item["edit_mode"] = "dialogue_bed"
    elif fmt == "gta6":
        item["edit_format"] = "gta6"
        item["edit_mode"] = item.get("edit_mode") or "music_only"
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
