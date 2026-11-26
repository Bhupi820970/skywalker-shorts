"""Durable posted-content history: no video, and no clip from the same source scene, is posted twice.

Keys are content, never titles:
  clip:<scene asset>  every official scene clip merged into a posted video
                      (render_item merges up to 5 same-topic clips, so all of them count)
  asset:<mp4 name>    the rendered Short itself (stock copies share the stock mp4)
  sha:<sha256>        the uploaded bytes (recorded by yt_upload)

Sources of truth (both are durable in git):
  * config/posted_history.json - appended on every upload; seeded from past posts
    with each legacy post's whole topic clip set (old results only kept the first clip)
  * every results/*.json that was posted (youtube_url / Buffer sent or scheduled)

The 20261117 and 20261124 Evil Eye Shorts were both cut from dandadanevil_scene1;
the old guard only blocked the same source for 72h and compared titles.
"""
from __future__ import annotations

import glob
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
HISTORY = "config/posted_history.json"
IST = timezone(timedelta(hours=5, minutes=30))
_SCENE_RE = re.compile(r"^(?P<topic>[a-z0-9]+)_scene\d+_h264\.mp4$")


def clip_name(u) -> str:
    """Normalized scene-clip key: release asset basename, or the bare URL for link sources."""
    s = str(u or "").strip()
    if not s:
        return ""
    if s.startswith("gh:"):
        s = s[3:]
    low = s.lower().split("?")[0].rstrip("/")
    base = low.rsplit("/", 1)[-1]
    if base.endswith(".mp4"):
        return base
    return low


def scene_topic(clip: str) -> str:
    m = _SCENE_RE.match(clip_name(clip))
    return m.group("topic") if m else ""


def _not_real(r: dict) -> bool:
    iid = str(r.get("id") or "")
    return bool(r.get("no_publish") or r.get("demo") or r.get("dry_run")
                or iid.startswith(("demo-", "test-"))
                or "/test-renders/" in str(r.get("asset_url") or ""))


def is_posted(r: dict | None) -> bool:
    """True once a result is on the channel (or scheduled there)."""
    if not r or _not_real(r):
        return False
    if r.get("youtube_url") or r.get("yt_video_id"):
        return True
    if r.get("publish_blocked"):
        return False
    return bool(r.get("buffer_status") in ("sent", "scheduled") or r.get("buffer_posted_at"))


def result_clips(r: dict | None) -> list[str]:
    r = r or {}
    raw = list(r.get("source_bundle") or [])
    if r.get("source"):
        raw.append(r["source"])
    out: list[str] = []
    for u in raw:
        c = clip_name(u)
        if c and c not in out:
            out.append(c)
    return out


def asset_name(r: dict | None) -> str:
    u = str((r or {}).get("asset_url") or "")
    return clip_name(u) if u else ""


def keys_of(r: dict | None, sha: str | None = None) -> set[str]:
    r = r or {}
    keys = {"clip:" + c for c in result_clips(r)}
    a = asset_name(r)
    if a:
        keys.add("asset:" + a)
    for h in (sha, r.get("asset_sha256")):
        if h:
            keys.add("sha:" + str(h).lower())
    return keys


def load_history(root: Path | None = None) -> dict:
    p = Path(root or REPO) / HISTORY
    try:
        h = json.loads(p.read_text())
        if isinstance(h, dict) and isinstance(h.get("items"), list):
            return h
    except Exception:
        pass
    return {"items": []}


def save_history(h: dict, root: Path | None = None) -> None:
    p = Path(root or REPO) / HISTORY
    p.parent.mkdir(parents=True, exist_ok=True)
    h["items"] = sorted(h.get("items") or [], key=lambda e: (str(e.get("at") or ""), str(e.get("id") or "")))
    p.write_text(json.dumps(h, indent=2, ensure_ascii=False) + "\n")


def iter_results(root: Path | None = None) -> list[dict]:
    out = []
    for p in sorted(glob.glob(str(Path(root or REPO) / "results" / "*.json"))):
        try:
            r = json.loads(Path(p).read_text())
        except Exception:
            continue
        if isinstance(r, dict):
            r.setdefault("id", Path(p).stem)
            out.append(r)
    return out


def index(root: Path | None = None, rows: list[dict] | None = None) -> dict[str, str]:
    """content key -> id of the posted video that owns it (history file + posted results)."""
    idx: dict[str, str] = {}
    for e in load_history(root).get("items") or []:
        owner = str(e.get("id") or e.get("youtube_url") or "?")
        for k in e.get("keys") or []:
            idx.setdefault(k, owner)
    for r in rows if rows is not None else iter_results(root):
        if is_posted(r):
            for k in keys_of(r):
                idx.setdefault(k, str(r.get("id")))
    return idx


def _related(r: dict, owner: str) -> bool:
    """The owner is this very result (or the stock entry it was copied from / to)."""
    return owner in {str(r.get("id") or ""), str(r.get("stock_id") or ""),
                     str(r.get("assigned_result") or "")} - {""}


def conflict(r: dict | None, idx: dict[str, str], sha: str | None = None) -> str | None:
    """Reason string when r would re-post a clip / video already on the channel, else None."""
    r = r or {}
    for k in sorted(keys_of(r, sha)):
        owner = idx.get(k)
        if owner and not _related(r, owner):
            kind, _, val = k.partition(":")
            what = {"clip": "same source scene", "asset": "same video file", "sha": "same video bytes"}[kind]
            return f"{what} already posted ({owner}): {val}"
    return None


def record(r: dict, root: Path | None = None, sha: str | None = None,
           now: datetime | None = None, extra_clips: list[str] | None = None) -> dict:
    """Append (or refresh) a posted video in the durable history file."""
    h = load_history(root)
    iid = str(r.get("id") or "")
    keys = keys_of(r, sha) | {"clip:" + clip_name(c) for c in (extra_clips or []) if clip_name(c)}
    ent = next((e for e in h["items"] if e.get("id") == iid), None)
    if ent is None:
        ent = {"id": iid}
        h["items"].append(ent)
    ent.update({
        "at": ent.get("at") or (now or datetime.now(IST)).strftime("%Y-%m-%d %H:%M IST"),
        "slot": r.get("slot"),
        "title": (r.get("title") or "")[:140],
        "youtube_url": r.get("youtube_url"),
        "keys": sorted(set(ent.get("keys") or []) | keys),
    })
    save_history(h, root)
    return ent


def blocked_clips(root: Path | None = None, rows: list[dict] | None = None,
                  exclude_ids: set[str] | None = None, reserved: bool = True) -> set[str]:
    """Scene clips a new render must not use: posted ones, plus (reserved=True) clips
    already inside a rendered video that has not gone out yet, and clips the clip ledger
    marks used or reserved by a running render."""
    rows = rows if rows is not None else iter_results(root)
    exclude_ids = exclude_ids or set()
    out = {k[5:] for k, owner in index(root, rows).items()
           if k.startswith("clip:") and owner not in exclude_ids}
    if reserved:
        out |= reserved_clips(rows, exclude_ids, ready_ids=_ready_stock_ids(root))
        try:   # atomic clip ledger (clip_reserve.py): used by a rendered Short, or held by a live render
            import clip_reserve
            out |= {k for k in clip_reserve.ledger_taken(exclude_videos=exclude_ids) if k.endswith(".mp4")}
        except Exception:
            pass
    return out


def _ready_stock_ids(root: Path | None = None) -> set[str] | None:
    try:
        man = json.loads((Path(root or REPO) / "config" / "backup_stock.json").read_text())
    except Exception:
        return None
    return {e.get("id") for e in man.get("items") or [] if e.get("status") == "ready"}


def reserved_clips(rows: list[dict], exclude_ids: set[str] | None = None,
                   ready_ids: set[str] | None = None) -> set[str]:
    """Clips held by unposted, publishable renders: ready stock (per the manifest when
    `ready_ids` is given), stock copies assigned to a slot that has not posted yet, and
    rendered queue Shorts still waiting for their slot."""
    exclude_ids = exclude_ids or set()
    out: set[str] = set()
    for r in rows:
        iid = str(r.get("id") or "")
        if iid in exclude_ids or _not_real(r) or is_posted(r):
            continue
        if r.get("status") != "done" or r.get("publish_blocked"):
            continue
        if r.get("assigned_from_stock"):
            out |= set(result_clips(r))
        elif iid.startswith("stock-"):
            if not r.get("stock_used") and (ready_ids is None or iid in ready_ids):
                out |= set(result_clips(r))
        else:   # rendered queue Short waiting for its slot: its clips are taken too
            out |= set(result_clips(r))
    return out


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def seed(root: Path | None = None, release_assets: set[str] | None = None,
         now: datetime | None = None) -> dict:
    """Build/refresh history from every posted result. Legacy results (no source_bundle)
    merged every same-topic scene clip, so the whole topic clip set on the release counts."""
    root = Path(root or REPO)
    h = load_history(root)
    by_id = {e.get("id"): e for e in h["items"]}
    assets = {clip_name(a) for a in (release_assets or set())}
    for r in iter_results(root):
        if not is_posted(r):
            continue
        keys = keys_of(r)
        if not r.get("source_bundle"):
            for c in result_clips(r):
                t = scene_topic(c)
                if t:
                    keys |= {"clip:" + a for a in assets if scene_topic(a) == t}
        ent = by_id.get(r["id"]) or {"id": r["id"]}
        ent.update({"at": ent.get("at") or str(r.get("yt_uploaded_at") or r.get("buffer_posted_at")
                                                  or r.get("slot") or r.get("at") or ""),
                    "slot": r.get("slot"), "title": (r.get("title") or "")[:140],
                    "youtube_url": r.get("youtube_url"),
                    "keys": sorted(set(ent.get("keys") or []) | keys)})
        by_id[r["id"]] = ent
    h["items"] = list(by_id.values())
    h["note"] = ("Content already on the channel. Publish, stock assignment and renders refuse any "
                 "clip:/asset:/sha: key listed here. Append-only; never delete entries.")
    save_history(h, root)
    return h


if __name__ == "__main__":
    import sys
    names = set()
    if len(sys.argv) > 1:
        names = {ln.strip() for ln in open(sys.argv[1]) if ln.strip()}
    h = seed(REPO, names)
    print(f"{len(h['items'])} posted videos in {HISTORY}")
