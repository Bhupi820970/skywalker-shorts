#!/usr/bin/env python3
"""Render one queue item: download -> pipeline.py (+QA) -> 4K lanczos -> Release -> results/<id>.json.

Automatic retry ladder (self-heal, no bot required):
  1) normal (yt-dlp, then official fallbacks)
  2) force the sources-release fallback mp4
  3) fallback mp4 + a different credited music track + alternate scene
     selection + relaxed NON-hard-rule thresholds
     (never relaxes no-crop, layout, or no-logo/title)

Any in-job failure (short clean footage, no_crop/letterbox, logo, sub-1080,
white-bar/caption, leftover dialogue, download/bot-check/404, music/beat,
QC, or publish gate) first advances to the next official clip of the same
title, then to the next same-format official title (romance → romance,
action → action), then to a backup-pool title. Demo and forced-id items
follow the same ladder; the original id / no_publish flag is kept. A
mid-render runner shutdown stays on the same title so the chunked path can
retry it.

If every candidate also fails and results/<id>.json has not yet used its
outer auto-retry, we write status=retry_queued and exit 0. A companion
workflow re-dispatches the item once more later. Only after that last
attempt fails do we write status=failed and exit 1 (that is what wakes BIBI).
"""
import glob, json, os, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
REPO = Path(__file__).resolve().parent.parent
iid = sys.argv[1]

def _retired_topic_exit(rid, reason):
    """Pool topic was removed: write a terminal result (no more auto-retry dispatches) and exit 0."""
    from datetime import datetime as _dt, timezone as _tz
    _res = REPO / "results"
    _res.mkdir(exist_ok=True)
    _p = _res / f"{rid}.json"
    try:
        _prev = json.loads(_p.read_text()) if _p.exists() else {}
    except Exception:
        _prev = {}
    _prev.update({"id": rid, "status": "failed", "retired": True, "detail": reason, "last_error": reason,
                  "auto_retries": int(_prev.get("max_auto_retries") or 1),
                  "at": _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    _p.write_text(json.dumps(_prev, indent=2, ensure_ascii=False))
    _o = REPO / "out" / "results"
    _o.mkdir(parents=True, exist_ok=True)
    (_o / f"{rid}.json").write_text(json.dumps(_prev, indent=2, ensure_ascii=False))
    print(f"::warning::{rid}: {reason}; marked failed/retired (no retry)")
    sys.exit(0)

BACKUP = None
STOCK = None
if iid.startswith("stock-"):
    # stock-<slug>-<nn> -> render a pool topic with no slot, upload to backup-stock.
    import re as _re
    _m = _re.match(r"^stock-(.+)-(\d{2})$", iid)
    if not _m:
        sys.exit(f"bad stock id {iid}")
    _slug = _m.group(1)
    _pool = json.load(open(REPO / "config" / "backup_pool.json")).get("items") or []
    STOCK = next((dict(b) for b in _pool if b.get("id") == f"backup-{_slug}"), None)
    if STOCK is None:
        _retired_topic_exit(iid, f"stock topic backup-{_slug} not in backup_pool (retired)")
    STOCK.pop("slot", None)
    STOCK["id"] = iid
    STOCK["stock"] = True
    STOCK["stock_topic"] = f"backup-{_slug}"
    STOCK["backup_item"] = f"backup-{_slug}"
    STOCK["no_publish"] = False
    STOCK["spare"] = True
elif "@" in iid:
    # backup-<slug>@YYYYMMDD-HHMM -> render pool item for that slot as YYYYMMDD-HHMM-<slug>
    _bid, _code = iid.split("@", 1)
    _pool = json.load(open(REPO / "config" / "backup_pool.json")).get("items") or []
    BACKUP = next((dict(b) for b in _pool if b["id"] == _bid), None)
    if BACKUP is None:
        _retired_topic_exit(f"{_code}-{_bid[len('backup-'):]}", f"backup item {_bid} not in backup_pool (retired)")
    _d, _t = _code.split("-")
    BACKUP["slot"] = f"{_d[:4]}-{_d[4:6]}-{_d[6:]}T{_t[:2]}:{_t[2:]}:00+05:30"
    BACKUP["backup_item"] = _bid
    iid = f"{_code}-{_bid[len('backup-'):]}"
    BACKUP["id"] = iid
OUT = REPO / "out"; OUT.mkdir(exist_ok=True)
RES = REPO / "results"; RES.mkdir(exist_ok=True)
run_url = f"{os.environ.get('GITHUB_SERVER_URL','https://github.com')}/{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/{os.environ.get('GITHUB_RUN_ID','')}"
MAX_AUTO_RETRIES = 1  # outer workflow_run re-dispatch budget

item = STOCK or BACKUP
for f in ([] if (STOCK or BACKUP) else sorted(glob.glob(str(REPO / "queue" / "*.json")))):
    for it in json.load(open(f)):
        if it["id"] == iid:
            item = it
if item is None:
    sys.exit(f"item {iid} not found")

# Hard rule: white-bar hook and title must be pure English (no Hindi/Hinglish).
# Body subtitles may still follow the trailer audio. Fail before any download
# so a bad hook is never baked into a render; do not write results/<id>.json
# so the next cron can pick the item up after the queue text is fixed.
sys.path.insert(0, str(REPO))
import hook_text
_hook_errs = hook_text.check_item_hook_fields(item)
if _hook_errs:
    sys.exit("English-only hook/title failed: " + "; ".join(_hook_errs))

import gate
import gemini
import sky_ai
import overlay_guard
import chunked
import sourcing
import title_swap
import edit_format

import posted_history

# Never re-use a scene clip that is already in a posted Short (all time). Stock renders also
# skip clips held by another ready / assigned stock video, so backup stock stays diverse.
try:
    BLOCKED_SOURCES = posted_history.blocked_clips(
        REPO, exclude_ids={iid}, reserved=bool(item.get("stock")))
except Exception as _e:  # history unreadable: publish-time guard still blocks repeats
    print(f"::warning::posted history unavailable: {_e}", flush=True)
    BLOCKED_SOURCES = set()
if BLOCKED_SOURCES:
    print(f"posted-history: {len(BLOCKED_SOURCES)} scene clip(s) already posted/reserved are skipped", flush=True)

os.environ.setdefault("SKY_GEMINI_BUDGET_FILE", str(OUT / f"gemini_budget_{iid}.json"))
QUEUE_META = {k: item.get(k) for k in ("hook", "title", "description", "tags")}
GEMINI_META = None
OUT_RES = int(item.get("out_res") or os.environ.get("SKY_OUT_RES") or 2160)


def fetch_music_library():
    """Download the trending-song mp3s from the 'music' release into music/lib (CI only, cheap)."""
    lp = REPO / "music" / "library.json"
    if not lp.exists():
        return 0
    lib = json.loads(lp.read_text())
    ldir = REPO / (lib.get("dir") or "music/lib")
    ldir.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in ldir.glob("*.mp3")}
    need = [t["file"] for t in lib.get("tracks") or [] if t["file"] not in have]
    repo = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/skywalker-shorts")
    if need:
        # Only ask for files that are on the release (a lost/restoring track must not fail the whole download).
        r = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/releases/tags/{lib.get('release') or 'music'}",
                            "-q", ".assets[].name"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            on_release = set(r.stdout.split())
            skipped = [f for f in need if f not in on_release]
            if skipped:
                print(f"music library: {len(skipped)} track(s) not on the release yet (box restores them); skipped")
            need = [f for f in need if f in on_release]
    if need:
        args = ["gh", "release", "download", lib.get("release") or "music", "-R", repo, "-D", str(ldir), "--clobber"]
        for f in need:
            args += ["-p", f]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            print("music library download failed (falling back to credits.json):", (r.stderr or "")[-300:])
    n = len(list(ldir.glob("*.mp3")))
    print(f"music library: {n} track(s) in {ldir}")
    return n


def recent_music(window=None):
    """Songs used in the last 3 days (plus same-title copies) so a track is not repeated."""
    import music_lib
    days = 3
    if window:
        # legacy arg: treat a small window as days if it looks like a day count
        try:
            days = int(window) if int(window) <= 14 else 3
        except Exception:
            days = 3
    return music_lib.recent_excludes(RES, REPO, days=days, exclude_id=iid)


def now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

def read_prev():
    p = RES / f"{iid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}

# Every per-source failure of this job (attempts only keeps the last one per attempt); feeds the
# per-clip quarantine in title_swap.release_asset_health.
SOURCE_FAILS = []


def _note_source_fail(last):
    try:
        SOURCE_FAILS.append({"source": last.get("source"), "error": str(last.get("error") or "")[:300],
                             "at": now()})
    except Exception:
        pass


def write(status, detail, **extra):
    r = {"id": iid, "status": status, "detail": detail, "at": now(), "run_url": run_url,
         "slot": item.get("slot"), "title": item.get("title"), "hook": item.get("hook"),
         "description": item.get("description"), "tags": item.get("tags"),
         "queue_meta": QUEUE_META if GEMINI_META else None, "gemini_meta": bool(GEMINI_META),
         "hook_source": (GEMINI_META or {}).get("source"), "hook_style": (GEMINI_META or {}).get("style"), "title_candidates": (GEMINI_META or {}).get("title_candidates"),
         "gemini_calls": gemini._budget().get("log", []), "backup_item": item.get("backup_item"),
         "stock": item.get("stock"), "stock_topic": item.get("stock_topic"), "spare": item.get("spare"),
         "words": item.get("words"), "edit_format": item.get("edit_format"),
         "title_swaps": item.get("title_swaps") or None,
         "swapped_from": item.get("swapped_from"), "swapped_to": item.get("swapped_to"), **extra}
    # HARD guard: the title/description/hashtags must be for the show whose clips are in the video.
    mismatch = title_swap.footage_title_mismatch(item, r.get("source"))
    if mismatch:
        r["footage_mismatch"] = mismatch
        if status == "done":
            print(f"::warning::{iid}: {mismatch}; refusing to mark it ready", flush=True)
            r["status"] = "failed"
            r["detail"] = f"{mismatch} (refused; was: {detail})"[:900]
            r["last_error"] = mismatch
    if SOURCE_FAILS and "source_attempts" not in r:
        r["source_attempts"] = SOURCE_FAILS[-30:]
    prev = read_prev()
    for k in ("buffer_post_id", "buffer_status", "buffer_due_at", "buffer_posted_at", "youtube_url"):
        if prev.get(k) and k not in r:
            r[k] = prev[k]  # never lose the Buffer record (idempotency)
    (RES / f"{iid}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    (OUT / "results").mkdir(exist_ok=True)  # artifact copy (backup ids differ from the matrix id)
    (OUT / "results" / f"{iid}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return r

def probe_wh(p):
    o = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=width,height", "-of", "csv=p=0", str(p)], text=True).strip()
    return [int(x) for x in o.split(",")[:2]]

def _ok_src(p):
    return p.exists() and p.stat().st_size > 500_000

def download_url(url, dest):
    """Fetch a direct mp4. GitHub release URLs use gh (token) first, then curl."""
    dest.unlink(missing_ok=True)
    if "github.com" in url and "/releases/download/" in url:
        parts = url.split("/releases/download/")
        repo = parts[0].split("github.com/")[-1].strip("/")
        tag, asset = parts[1].split("/", 1)
        r = subprocess.run(["gh", "release", "download", tag, "-p", asset, "-R", repo, "-D", str(dest.parent), "--clobber"],
                           capture_output=True, text=True)
        cand = dest.parent / asset
        if r.returncode == 0 and _ok_src(cand):
            if cand.resolve() != dest.resolve():
                dest.write_bytes(cand.read_bytes())
            return True, f"gh:{url}"
    r = subprocess.run(["curl", "-fsSL", "--retry", "3", "-L", "-o", str(dest), url], capture_output=True, text=True)
    if r.returncode == 0 and _ok_src(dest):
        return True, url
    return False, f"direct mp4 {url}: curl exit {r.returncode} {(r.stderr or '')[-200:]}"

def _src_key(info: str) -> str:
    return (info or "").rstrip("/").split("/")[-1].split("?")[0].lower()


def _is_skipped(info, skip) -> bool:
    if not info:
        return False
    skip = set(skip or [])
    if info in skip:
        return True
    key = _src_key(info)
    return any(_src_key(s) == key and key for s in skip)


def download_fallback(src, skip=None):
    errors = []
    used = None
    skip = set(skip or [])
    for u in item.get("fallback_mp4", []) or []:
        if sourcing.is_trailer_source(u) or sourcing.is_unusable_source(u, item):
            continue
        if _is_skipped(u, skip) or _is_skipped(f"gh:{u}", skip):
            continue
        ok, info = download_url(u, src)
        if ok:
            return info, []
        errors.append(info)
    if item.get("fallback_release") and item.get("fallback_asset") and not sourcing.is_trailer_source(item.get("fallback_asset") or "") and not sourcing.is_unusable_source(item.get("fallback_asset") or "", item):
        tag, asset = item["fallback_release"], item["fallback_asset"]
        repo = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/skywalker-shorts")
        r = subprocess.run(["gh", "release", "download", tag, "-p", asset, "-R", repo, "-D", str(OUT), "--clobber"],
                           capture_output=True, text=True)
        cand = OUT / asset
        if r.returncode == 0 and _ok_src(cand):
            if cand.resolve() != src.resolve():
                src.write_bytes(cand.read_bytes())
            return f"gh:release/{tag}/{asset}", []
        errors.append("gh release " + (r.stderr.strip().splitlines() or ["?"])[-1][:300])
    # last resort: sources release, guess asset from fallback_mp4 names or id
    repo = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/skywalker-shorts")
    guesses = []
    if item.get("fallback_asset") and not sourcing.is_trailer_source(item.get("fallback_asset") or "") and not sourcing.is_unusable_source(item.get("fallback_asset") or "", item):
        guesses.append(item["fallback_asset"])
    for u in item.get("fallback_mp4") or []:
        if not sourcing.is_trailer_source(u) and not sourcing.is_unusable_source(u, item):
            guesses.append(u.rstrip("/").split("/")[-1])
    for g in guesses:
        if sourcing.is_trailer_source(g):
            continue
        r = subprocess.run(["gh", "release", "download", "sources", "-p", g, "-R", repo, "-D", str(OUT), "--clobber"],
                           capture_output=True, text=True)
        cand = OUT / g
        if r.returncode == 0 and _ok_src(cand):
            if cand.resolve() != src.resolve():
                src.write_bytes(cand.read_bytes())
            return f"gh:release/sources/{g}", []
        errors.append("gh sources " + g + " " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    return None, errors


def _stem_tokens(name: str) -> set[str]:
    stem = (name or "").rsplit(".", 1)[0].lower()
    for junk in ("h264", "trailer", "teaser", "official", "src", "video", "4k"):
        stem = stem.replace(junk, " ")
    parts = [x for x in stem.replace("-", "_").replace(".", "_").split("_") if len(x) >= 3]
    return set(parts)


def list_source_siblings():
    """Other official SCENE clips already uploaded to the sources release (never trailers)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "Bhupi820970/skywalker-shorts")
    r = subprocess.run(
        ["gh", "release", "view", "sources", "-R", repo, "--json", "assets"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    try:
        assets = [a.get("name") for a in (json.loads(r.stdout).get("assets") or []) if a.get("name")]
    except Exception:
        return []
    seeds = []
    if item.get("fallback_asset"):
        seeds.append(item["fallback_asset"])
    for u in item.get("fallback_mp4") or []:
        seeds.append(u.rstrip("/").split("/")[-1])
    already = set(seeds)
    out = []
    for name in assets:
        if name in already or not str(name).lower().endswith(".mp4"):
            continue
        if sourcing.is_trailer_source(name) or sourcing.is_unusable_source(name, item):
            continue
        if sourcing.source_matches_topic(item, name):
            out.append(
                f"https://github.com/{repo}/releases/download/sources/{name}"
            )
    out.sort(key=lambda u: 0 if "_scene" in u.rsplit("/", 1)[-1] else 1)
    return out


def _concat_scene_files(paths, dest):
    """Join 2+ official scene clips of the same title into one source file."""
    if not paths:
        return False
    dest.unlink(missing_ok=True)
    if len(paths) == 1:
        srcp = Path(paths[0])
        if srcp.resolve() != dest.resolve():
            dest.write_bytes(srcp.read_bytes())
        return _ok_src(dest)
    lst = dest.parent / f"{dest.stem}_concat.txt"
    lst.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in paths))
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(dest)],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and _ok_src(dest):
        return True
    dest.unlink(missing_ok=True)
    # Same size/fps so concat is valid; pad (never crop) to 1920x1080.
    parts = []
    for i, pth in enumerate(paths):
        q = dest.parent / f"{dest.stem}_n{i}.mp4"
        q.unlink(missing_ok=True)
        rr = subprocess.run(
            ["ffmpeg", "-y", "-i", str(pth),
             "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", str(q)],
            capture_output=True, text=True,
        )
        if rr.returncode == 0 and _ok_src(q):
            parts.append(q)
    if not parts:
        return False
    lst.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in parts))
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(dest)],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and _ok_src(dest)


MAX_SCENE_CLIPS = 5


LAST_BUNDLE = []  # clips merged into the last downloaded bundle


def download_scene_bundle(src, skip=None):
    """Download up to 5 official scene clips of this topic and merge them.

    Trailers are never downloaded. Topics with no scene clips fail.
    All same-title official scene clips are merged before giving up.
    """
    errors = []
    skip = set(skip or [])
    urls = []
    for u in list(item.get("fallback_mp4") or []) + list(item.get("sources") or []):
        if u and not sourcing.is_trailer_source(u) and u not in urls:
            urls.append(u)
    for u in list_source_siblings():
        if u not in urls and not sourcing.is_trailer_source(u):
            urls.append(u)
    allow_yt = os.environ.get("SKY_ALLOW_YOUTUBE", "1") != "0" and not sourcing.release_only()
    for u in sourcing.build_candidate_urls(item, include_youtube=allow_yt):
        if u and not sourcing.is_trailer_source(u) and u not in urls:
            urls.append(u)
    # When the known set is thin, search more official same-title clips
    # (studio/distributor channels only; unofficial re-uploads never pass).
    if len(urls) < MAX_SCENE_CLIPS:
        extra = sourcing.more_official_scene_urls(item, already=urls, limit=MAX_SCENE_CLIPS)
        for u in extra:
            if u and u not in urls and not sourcing.is_trailer_source(u):
                urls.append(u)
    urls = [u for u in urls if not _is_skipped(u, skip) and not _is_skipped(f"gh:{u}", skip)]
    if sourcing.release_only():
        # GitHub never downloads from YouTube/X (bot wall). Only clips the box already put on the
        # `sources` release are used; anything else is the box's job (config/scene_requests.json).
        have = sourcing.release_source_assets()
        usable = title_swap.usable_release_assets(have, REPO)
        if have is not None and usable is not None and len(usable) < len(have):
            quarantined = [u for u in urls if sourcing.is_release_url(u)
                           and u.rsplit("/", 1)[-1] in have and u.rsplit("/", 1)[-1] not in usable]
            if quarantined:
                errors.append(f"quarantined {len(quarantined)} clip(s) that keep failing the logo/scene filter: "
                              + ", ".join(u.rsplit("/", 1)[-1] for u in quarantined)[:300])
            urls = [u for u in urls if u not in quarantined]
            have = usable
        rel = [u for u in urls if sourcing.is_release_url(u)]
        missing = [u for u in urls if u not in rel]
        if have is not None:
            missing += [u for u in rel if u.rsplit("/", 1)[-1] not in have]
            rel = [u for u in rel if u.rsplit("/", 1)[-1] in have]
        if missing:
            errors.append(f"release-only: {len(missing)} clip(s) not on the sources release yet (box fetches them)")
        urls = rel
    urls.sort(key=sourcing._rank_url)
    paths, useds = [], []
    for u in urls:
        if len(paths) >= MAX_SCENE_CLIPS:
            break
        link_page = sourcing.is_youtube_url(u) or sourcing.is_x_url(u)
        if not sourcing.source_matches_topic(item, u) and not link_page:
            # github/release assets must match this title; search URLs are checked after download
            if "github.com" in (u or "") or (u or "").lower().endswith(".mp4"):
                continue
        part = src.parent / f"{src.stem}_p{len(paths)+1}.mp4"
        part.unlink(missing_ok=True)
        ok, info = (False, "")
        is_direct = (
            (u or "").lower().endswith(".mp4")
            or ("github.com" in u and "/releases/download/" in u)
            or "archive.org/download/" in u
        )
        if is_direct:
            ok, info = download_url(u, part)
        if not ok and not sourcing.release_only():
            ok, info = sourcing.download_ytdlp(u, part, allow_youtube=allow_yt)
        if not ok or not _ok_src(part):
            errors.append(info or f"fail:{u}")
            part.unlink(missing_ok=True)
            continue
        if not sourcing.source_matches_topic(item, info) and not sourcing.source_matches_topic(item, u):
            # Keep non-asset page downloads (unique official clip, no foreign slug in the name).
            if sourcing.source_topic_slug(info) or sourcing.source_topic_slug(u):
                errors.append(f"skip different-topic source: {info}")
                part.unlink(missing_ok=True)
                continue
        why = overlay_guard.clip_reason(part)
        if why:
            errors.append(f"reject {why}: {info or u}")
            part.unlink(missing_ok=True)
            continue
        ok_res, why_res = sourcing.resolution_ok(part)
        if not ok_res:
            errors.append(f"reject {why_res}: {info or u}")
            part.unlink(missing_ok=True)
            continue
        sourcing.trim_source_length(part)
        print(f"  scene clip {len(paths)+1}: {info}", flush=True)
        paths.append(part)
        useds.append(info)
        total_s = sum(sourcing._probe_duration(x) for x in paths)
        if total_s >= 210:
            print(f"  scene bundle cap reached ({total_s:.0f}s >= 210s); not adding more clips", flush=True)
            break
    if errors:
        print("  scene clip skips: " + " | ".join(str(e) for e in errors[:8])[:800], flush=True)
    if not paths:
        return None, errors or ["no official scene clips for this topic"]
    LAST_BUNDLE[:] = list(useds)
    if not _concat_scene_files(paths, src):
        return None, errors + ["failed to merge official scene clips"]
    # Per-clip trim is 180s, but two clips can still exceed the 210s bundle
    # cap (the cap only stops adding a third file). Hard-trim the merge.
    if sourcing.trim_source_length(src, 210.0):
        print(f"  trimmed merged scene bundle to 210s", flush=True)
    return useds[0], errors


def download_source(src, force_fallback=False, skip=None):
    """Official scene clips only. Link mode is primary: official X, then
    official YouTube, then the sources-release / backup pool. Merges up to
    5 same-title scene clips. Trailers/teasers are never used.
    """
    errors = []
    skip = set(skip or []) | set(BLOCKED_SOURCES)
    used, berr = download_scene_bundle(src, skip=skip)
    if not used and BLOCKED_SOURCES and any(
            posted_history.clip_name(u) in BLOCKED_SOURCES for u in sourcing.item_scene_urls(item)):
        berr = list(berr) + ["no unused official source left for this topic "
                             "(scene clips already posted on the channel)"]
    errors.extend(berr)
    if used and not _is_skipped(used, skip) and not sourcing.is_trailer_source(used):
        if sourcing.source_matches_topic(item, used) or not sourcing.source_topic_slug(used):
            return used, errors
        errors.append(f"skip different-topic source: {used}")
    return None, errors or ["no official scene clips for this topic"]


def _try_pexels(dest):
    key = os.environ.get("PEXELS_API_KEY")
    q = (item.get("pexels_query") or "").strip()
    if not key or not q:
        return None
    try:
        import json as _json, urllib.parse, urllib.request
        req = urllib.request.Request(
            "https://api.pexels.com/videos/search?" + urllib.parse.urlencode({"query": q, "per_page": 5, "orientation": "landscape"}),
            headers={"Authorization": key},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            data = _json.loads(r.read().decode())
        files = []
        for v in data.get("videos") or []:
            for f in v.get("video_files") or []:
                if (f.get("file_type") or "").endswith("mp4") and (f.get("width") or 0) >= 1280:
                    files.append(f["link"])
        for url in files[:3]:
            ok, _ = download_url(url, dest)
            if ok:
                return f"pexels:{q}"
    except Exception as e:
        print("pexels skipped:", e)
    return None

def publish(final, fw, fh, up, used, qa, credit, qa_obj):
    desc = item.get("description", "")
    if credit.get("file") or credit.get("credit"):
        try:
            import music_lib
            ctext = music_lib.credit_block(credit)
        except Exception:
            ctext = credit.get("credit") or ""
        if ctext and "\n\nMusic:" not in desc:
            desc = desc.rstrip() + "\n\nMusic:\n" + ctext
    # Prefer safe-track credit; always keep license line for description.
    notes = f"{item.get('title','')}\n\n{desc}\n\nTags: {', '.join(item.get('tags', []))}\nSource: {used}\nRun: {run_url}"
    no_publish = (
        os.environ.get("SKY_NO_PUBLISH", "").lower() in ("1", "true", "yes")
        or os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
        or bool(item.get("demo"))
        or bool(item.get("no_publish"))
    )
    # Test / demo renders go to the test-renders release (never Buffer/YouTube).
    # Pre-rendered stock (no slot yet) goes to the shared backup-stock release.
    is_stock = bool(item.get("stock")) and not no_publish
    if no_publish:
        tag = "test-renders"
    elif is_stock:
        tag = "backup-stock"
    else:
        tag = f"render-{iid}"
    asset_name = final.name
    if no_publish or is_stock:
        if no_publish:
            asset_name = final.name if final.name.startswith("test-") else f"test-{iid}.mp4"
        else:
            asset_name = f"{iid}.mp4"
        if asset_name != final.name:
            tagged = final.parent / asset_name
            if tagged.resolve() != final.resolve():
                tagged.write_bytes(final.read_bytes())
            final = tagged
        if no_publish:
            notes = "[NO PUBLISH / DRY-RUN]\n" + notes
        else:
            notes = "[BACKUP STOCK / no slot until assigned]\n" + notes
    r = subprocess.run(["gh", "release", "view", tag], capture_output=True)
    if r.returncode != 0:
        if no_publish:
            title = "test renders"
        elif is_stock:
            title = "backup stock"
        else:
            title = f"render {iid}"
        subprocess.check_call(["gh", "release", "create", tag, str(final), "--title", title, "--notes", notes])
    else:
        subprocess.check_call(["gh", "release", "upload", tag, str(final), "--clobber"])
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    cover_url = None
    cover = None
    for cand in list(OUT.glob(f"work_{iid}*/cover.jpg")) + [Path(final).with_name("cover.jpg")]:
        if cand.exists() and cand.stat().st_size > 1000:
            cover = cand
            break
    if cover is not None:
        cover_name = Path(asset_name).stem + "-cover.jpg"
        uploaded = cover.with_name(cover_name)
        if uploaded.resolve() != cover.resolve():
            uploaded.write_bytes(cover.read_bytes())
        subprocess.run(["gh", "release", "upload", tag, str(uploaded), "--clobber"], check=False)
        cover_url = f"https://github.com/{repo}/releases/download/{tag}/{cover_name}"
    try:
        _sha = posted_history.sha256_file(final)
    except Exception:
        _sha = None
    out = dict(
        source_bundle=[posted_history.clip_name(u) for u in (LAST_BUNDLE or [used]) if u],
        asset_sha256=_sha,
        release_url=f"https://github.com/{repo}/releases/tag/{tag}",
        asset_url=f"https://github.com/{repo}/releases/download/{tag}/{asset_name}",
        qa=qa, drop_t=qa_obj.get("drop_t"), energy=qa_obj.get("energy"), source=used,
        music=credit.get("file"), music_credit=credit.get("credit"), description_final=desc,
        detail=f"rendered {fw}x{fh} ({up}), QA ok" + (" [no-publish]" if no_publish else ""),
        no_publish=no_publish,
    )
    if cover_url:
        out["cover_url"] = cover_url
    try:
        lays = sorted(OUT.glob(f"work_{iid}*/layout.json"), key=lambda q: q.stat().st_mtime)
        sc = json.loads(lays[-1].read_text()).get("story_captions") if lays else None
        if sc:
            out["caption_pattern"] = sc.get("pattern")
            out["caption_style"] = "neon"
    except Exception:
        pass
    return out

def run_pipeline(src, out, qa_json, work, *, relax=False, alt_scenes=False, exclude_music=None, music_salt=""):
    cmd = [sys.executable, str(REPO / "pipeline.py"), "--src", str(src), "--out", str(out),
           "--template", item.get("template", "whitebar"), "--hook", item["hook"],
           "--pillar", item.get("pillar", "movie"), "--item-id", iid, "--music", "auto",
           "--target-dur", str(item.get("target_dur", 32)), "--qa-json", str(qa_json)]
    if item.get("words"):
        cmd += ["--words", item["words"]]
    out_res = int(item.get("out_res") or os.environ.get("SKY_OUT_RES") or 2160)
    if out_res in (1080, 2160):
        cmd += ["--out-res", str(out_res)]
    if relax:
        cmd += ["--relax"]
    if alt_scenes:
        cmd += ["--alt-scenes"]
    excl_all = list(dict.fromkeys(list(exclude_music or []) + list(RECENT_MUSIC or [])))
    if excl_all:
        cmd += ["--exclude-music", ",".join(excl_all)]
    if music_salt:
        cmd += ["--music-salt", music_salt]
    # Owner rule: no dialogue anywhere; every format is song-only.
    mode = "music_only"
    item["edit_mode"] = mode
    if mode in ("music_only", "two_act", "dialogue_bed"):
        cmd += ["--edit-mode", mode]
    checkout = Path(__file__).resolve().parent.parent
    chunked.activate_for_source(src, item=item, repo=checkout)
    env = dict(os.environ, SKY_WORK=str(work), SKY_ROOT=str(OUT), SKY_MARKET=(item.get("market") or "").upper())
    if mode in ("music_only", "two_act", "dialogue_bed"):
        env["SKY_EDIT_MODE"] = mode
    env["SKY_EDIT_FORMAT"] = item.get("edit_format") or edit_format.infer_format(item, pillar=str(item.get("pillar") or ""))
    if item.get("music_genre"):
        # Force the song category (action / love / cinematic / hiphop), e.g. per-genre demos.
        env["SKY_MUSIC_GENRE"] = str(item["music_genre"]).strip().lower()
    if (item.get("market") or "").upper() == "IN" or item.get("dialogue_lang") in ("hi", "te", "ta"):
        # Hindi/South dubs: whisper-small output was garbled ("पेवरेट ग्रिकेट").
        # medium + forced language is far closer to the real lines (~3 min CPU).
        env["SKY_WHISPER_MODEL"] = item.get("whisper_model") or "medium"
        env["SKY_WHISPER_LANG"] = item.get("dialogue_lang") or "hi"
        env.setdefault("SKY_WHISPER_PROMPT", "")
    if not env.get("SKY_WHISPER_PROMPT") and "SKY_WHISPER_PROMPT" not in env:
        # Character/film names help whisper spell them right in the subtitles.
        names = item.get("whisper_prompt") or (
            (item.get("description") or "").split("\n")[0] + " " +
            " ".join(t for t in (item.get("tags") or []) if t not in ("edit", "shorts", "trending")))
        env["SKY_WHISPER_PROMPT"] = names.strip()[:600]
    # fresh work dir per attempt so reused scenes/edl cannot leak
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    rc = subprocess.call(cmd, env=env)
    qa = json.loads(qa_json.read_text()) if qa_json.exists() else {}
    credit = {}
    cpath = work / "music_credit.json"
    if cpath.exists():
        credit = json.loads(cpath.read_text())
    checks = {k: v.get("ok") if isinstance(v, dict) else v for k, v in (qa.get("checks") or {}).items()}
    return rc, qa, credit, checks

def upscale(src, out_1080, final):
    sw, sh = probe_wh(src)
    out_res = int(item.get("out_res") or os.environ.get("SKY_OUT_RES") or 2160)
    if out_res == 1080:
        # Keep the 1080x1920 master as the published file (no 4K upscale).
        import shutil
        shutil.copy2(out_1080, final)
        fw, fh = probe_wh(final)
        return fw, fh, "1080x1920 (no upscale)"
    if chunked.chunked_active():
        try:
            chunked.upscale_4k_chunked(out_1080, final)
            up = f"chunked lanczos {sw}x{sh} source -> 2160x3840" if min(sw, sh) < 2160 else "4K source, scaled to 2160x3840 (chunked)"
            fw, fh = probe_wh(final)
            return fw, fh, up
        except Exception as e:
            if chunked.refuse_single_pass():
                print(f"  chunked 4K upscale failed ({e}); not falling back to single-pass", flush=True)
                raise
            print(f"  chunked 4K upscale failed ({e}); falling back to single-pass", flush=True)
            final.unlink(missing_ok=True)
    subprocess.check_call(["ffmpeg", "-y", *chunked.ffmpeg_thread_args(), "-i", str(out_1080),
                           "-vf", "scale=2160:3840:flags=lanczos",
                           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                           "-c:a", "copy", "-movflags", "+faststart", str(final)])
    up = f"lanczos {sw}x{sh} source -> 2160x3840" if min(sw, sh) < 2160 else "4K source, scaled to 2160x3840"
    fw, fh = probe_wh(final)
    return fw, fh, up

def _is_short_failure(reason, qa) -> bool:
    blob = " ".join([
        str(reason or ""),
        " ".join(str(x) for x in (qa or {}).get("issues") or []),
        str((qa or {}).get("unique_duration") or ""),
    ]).lower()
    keys = (
        "hard minimum", "unique clean shots", "aim 25-32", "aim 20-28", "below target",
        "too short", "short unique", "not publishing a short cut",
        "duration ", "need ≥18", "need >=18", "need ≥20", "need >=20",
        "need ≥25", "need >=25", "hard minimum 18",
    )
    return any(k in blob for k in keys)



def qc_verdict(gem_qc) -> str:
    return gate.qc_verdict(gem_qc)


def last_gemini_key() -> str | None:
    return gemini.last_key_from_log(gemini._budget().get("log") or [])


def run_gemini_qc(out, work) -> dict | None:
    """Run Gemini QC, rotating to the next key when a reserved retry was requested."""
    if not gemini.available():
        return None
    try:
        return sky_ai.qc_judge(out, work, item.get("hook", ""), item.get("title", ""),
                               item.get("description", ""))
    except Exception as e:
        print("gemini QC skipped:", e)
        return None


def scenes_picked(work: Path) -> list[dict]:
    """Compact EDL + Gemini scores for the result json (used by demo reports)."""
    out = []
    try:
        edl = json.loads((work / "edl.json").read_text())
    except Exception:
        return out
    by_id = {}
    sp = work / "scenes_scored.json"
    if sp.exists():
        try:
            for s in json.loads(sp.read_text()) or []:
                if isinstance(s, dict) and s.get("id") is not None:
                    by_id[s["id"]] = s
        except Exception:
            pass
    for shot in edl:
        sid = shot.get("scene")
        if sid in (None, "end"):
            continue
        s = by_id.get(sid) or {}
        rec = {
            "id": sid,
            "src_start": shot.get("src_start"),
            "dur": shot.get("visible") or shot.get("dur"),
        }
        if s.get("gemini") is not None:
            rec["gemini_score"] = s.get("gemini")
        if s.get("reason"):
            rec["reason"] = s.get("reason")
        elif s.get("gemini") is not None:
            rec["reason"] = f"gemini score {s.get('gemini')}"
        out.append(rec)
    return out[:16]


def _qc_only_retry(prev, auto_retries):
    """Re-judge an already-rendered cut with the next Gemini key. No re-render."""
    url = prev.get("asset_url")
    if not url:
        return None
    print("=== QC-only retry (no re-render) ===", flush=True)
    # The kept render may be a swapped/backup show; judge and label it as THAT show.
    if title_swap.restore_swap_from_result(item, prev):
        _refresh_item_context()
        print(f"  QC retry: restored swapped title {prev.get('swapped_from')} -> {prev.get('swapped_to')} "
              f"({item.get('words')}): {item.get('title')!r}", flush=True)
    mismatch = title_swap.footage_title_mismatch(item, prev.get("source"))
    if mismatch:
        print(f"  QC retry: {mismatch}; rendering fresh instead of re-judging", flush=True)
        return "rerender"
    gemini.allow_qc_retry(after_key=gemini.last_key_from_log(prev.get("gemini_calls") or []))
    dest = OUT / f"render-{iid}.mp4"
    ok, info = download_url(url, dest)
    if not ok or not dest.exists():
        print(f"  QC retry: could not download previous render: {info}")
        return None
    work = OUT / f"work_{iid}_qc"
    work.mkdir(parents=True, exist_ok=True)
    prev_gate = prev.get("gate") if isinstance(prev.get("gate"), dict) else {}
    if prev.get("qc_status") == "pass" and isinstance(prev_gate.get("gemini_qc"), dict):
        gem_qc = prev_gate["gemini_qc"]  # QC already passed; only the logo verdict was missing
    else:
        gem_qc = run_gemini_qc(dest, work)
    verdict = qc_verdict(gem_qc)
    print(f"gemini QC (retry): {verdict}", json.dumps(gem_qc, ensure_ascii=False) if gem_qc else "")
    lg = None
    if verdict == "pass":
        # HARD: the whole-video logo scan must pass on THIS file before it is ever marked done.
        import logo_guard
        try:
            lg = logo_guard.scan_video(dest, work, layout=prev.get("layout"))
        except Exception as e:
            lg = {"ok": None, "error": str(e)}
        print("logo scan (retry):", json.dumps(lg, ensure_ascii=False)[:600])
        if lg.get("ok") is False:
            print("  logo found in kept render; dropping it and rendering fresh", flush=True)
            return "rerender"
        if lg.get("ok") is None:
            return "skipped"
    extra = {k: prev.get(k) for k in (
        "release_url", "asset_url", "cover_url", "qa", "drop_t", "energy", "source",
        "music", "music_credit", "description_final", "gate", "no_publish",
        "scenes_picked", "layout",
    ) if prev.get(k) is not None}
    attempts = list(prev.get("attempts") or [])
    attempts.append({"attempt": "qc_retry", "ok": verdict == "pass",
                     "error": None if verdict == "pass" else f"gemini QC {verdict}",
                     "source": prev.get("source"), "music": prev.get("music")})
    if verdict == "pass":
        g = extra.get("gate") or {}
        if isinstance(g, dict):
            g = dict(g)
            g["gemini_qc"] = gem_qc
            g["logo_scan"] = lg
            g["logo_verdict"] = True
            g["failures"] = []
            g["ok"] = True
            extra["gate"] = g
        write("done", (prev.get("detail") or "rendered") + " (QC retry pass)",
              auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
              attempts=attempts, last_error=None, qc_status="pass", **extra)
        return 0
    if verdict == "fail":
        write("failed", "gemini QC reject on retry: " + "; ".join(
            (gem_qc or {}).get("reasons") or (gem_qc or {}).get("low") or ["?"])[:800],
              auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
              attempts=attempts, last_error="gemini QC reject", qc_rejected=True,
              qc_status="fail", **extra)
        return 1
    # still skipped
    return "skipped"


def attempt(n, *, force_fallback=False, relax=False, alt_scenes=False, exclude_music=None, music_salt=""):
    src = OUT / f"{iid}_src.mp4"
    print(f"=== attempt {n}: force_fallback={force_fallback} relax={relax} alt_scenes={alt_scenes} ===", flush=True)
    tried = []
    last = None
    src_i = 0
    expand_rounds = 0
    prev_bundle = ()
    global SOURCES_RENDERED
    while True:
        if src_i > 0 or SOURCES_RENDERED > 0:
            if SOURCES_RENDERED >= MAX_SOURCES_PER_JOB:
                print(f"  source cap: {SOURCES_RENDERED} sources rendered this job; stopping fall-through", flush=True)
                return False, dict(last or {"attempt": n, "ok": False, "source": None, "music": None},
                                   error=f"source cap {MAX_SOURCES_PER_JOB} reached; last: "
                                         + str((last or {}).get("error") or "none")[:500], capped=True)
            if not _budget_left_for(SOURCE_EST_S[0]):
                return False, dict(last or {"attempt": n, "ok": False, "source": None, "music": None},
                                   error="time budget reached; last: " + str((last or {}).get("error") or "none")[:500],
                                   capped=True)
        t_src = time.monotonic()
        used, errors = download_source(src, force_fallback=force_fallback, skip=tried)
        if not used:
            if last:
                return False, last
            reason = "download failed: " + " | ".join(errors)
            return False, {"attempt": n, "ok": False, "error": reason, "source": None, "music": None}
        if _is_skipped(used, tried):
            if last:
                return False, last
            return False, {"attempt": n, "ok": False, "error": "no unused official source left",
                           "source": used, "music": None}
        if not sourcing.source_matches_topic(item, used):
            print(f"  skip different-topic source: {used}", flush=True)
            last = {"attempt": n, "ok": False,
                    "error": "no unused official source left for this topic",
                    "source": used, "music": None}
            continue
        tried.append(used)
        src_i += 1
        print(f"  source {src_i}: {used}", flush=True)

        out = OUT / f"{iid}_1080.mp4"
        qa_json = OUT / f"{iid}_qa.json"
        work = OUT / f"work_{iid}"
        suffix = n if src_i == 1 else f"{n}s{src_i}"
        if n > 1 or src_i > 1:
            out = OUT / f"{iid}_1080_a{suffix}.mp4"
            qa_json = OUT / f"{iid}_qa_a{suffix}.json"
            work = OUT / f"work_{iid}_a{suffix}"
        bundle = tuple(LAST_BUNDLE)
        if expand_rounds and bundle and bundle == prev_bundle:
            # The "fetch more clips" round added nothing new (e.g. YouTube extraction failed):
            # re-rendering the identical bundle can only fail the same way.
            print("  expanded bundle is identical (no new official clip downloaded); not re-rendering it", flush=True)
            expand_rounds = 99
            continue
        prev_bundle = bundle
        SOURCES_RENDERED += 1
        rc, qa, credit, checks = run_pipeline(
            src, out, qa_json, work,
            relax=relax, alt_scenes=alt_scenes,
            exclude_music=exclude_music, music_salt=music_salt,
        )
        SOURCE_EST_S[0] = max(SOURCE_EST_S[0], time.monotonic() - t_src)
        if rc != 0 or not out.exists():
            reason = f"pipeline/QA failed (exit {rc}): " + "; ".join((qa.get("issues") or [])[:5])[:800]
            if not reason.endswith(")") and not qa.get("issues"):
                reason = f"pipeline/QA failed (exit {rc})"
            last = {"attempt": n, "ok": False, "error": reason, "source": used,
                    "music": credit.get("file"), "qa": checks}
            _note_source_fail(last)
            if _is_short_failure(reason, qa):
                already = list(tried) + list(item.get("alt_source_urls") or []) + list(item.get("fallback_mp4") or [])
                extra = sourcing.more_official_scene_urls(item, already=already, limit=MAX_SCENE_CLIPS)
                if extra and expand_rounds < 2:
                    alts = list(item.get("alt_source_urls") or [])
                    for u in extra:
                        if u not in alts:
                            alts.append(u)
                    item["alt_source_urls"] = alts
                    # Remake the merge with the new official clips; do not drop the footage we already have.
                    if used in tried:
                        tried.remove(used)
                    expand_rounds += 1
                    print(f"  only short unique clean shots; fetching {len(extra)} more official clip(s) of this title (round {expand_rounds})", flush=True)
                    continue
                print(f"  source too short / under duration gate; no further official clips found; trying next official scene clip", flush=True)
                continue
            # Logo, no_crop/letterbox, sub-1080, auto_qc, download, and other
            # QA failures must try the next official clip of this title — not
            # abort the attempt on the first bad source.
            if title_swap.should_try_next_source(reason):
                print(f"  source failed; trying next official scene clip: {reason[:220]}", flush=True)
                continue
            return False, last
        hm = work / "hook_meta.json"
        if hm.exists():
            global GEMINI_META
            meta = json.loads(hm.read_text())
            item["hook"], item["title"], item["description"] = meta["hook"], meta["title"], meta["description"]
            item["tags"] = list(dict.fromkeys(meta.get("hashtags", []) + (item.get("tags") or [])))[:12]
            GEMINI_META = meta
            print(f"hook/title from clip ({meta.get('source')}): {meta['hook']!r} | {meta['title']!r}")
        item["_avoid_openers"] = AVOID_OPENERS
        gem_qc = run_gemini_qc(out, work)
        verdict = qc_verdict(gem_qc)
        if verdict == "skipped":
            gemini.allow_qc_retry(after_key=last_gemini_key())
            gem_qc = run_gemini_qc(out, work)
            verdict = qc_verdict(gem_qc)
        if gem_qc is not None:
            print("gemini QC:", json.dumps(gem_qc, ensure_ascii=False))
        if verdict == "fail":
            last = {"attempt": n, "ok": False, "source": used, "music": credit.get("file"), "qa": checks,
                    "qc_status": "fail",
                    "error": "gemini QC reject: " + "; ".join(gem_qc.get("reasons") or gem_qc.get("low") or ["?"])[:600]}
            _note_source_fail(last)
            print(f"  gemini QC reject; trying next official scene clip: {last['error'][:220]}", flush=True)
            continue
        try:
            final = OUT / f"render-{iid}.mp4"
            fw, fh, up = upscale(src, out, final)
            if verdict == "skipped":
                # Keep the cut for a later QC retry with the next key. Never mark pass/ready.
                extra = publish(final, fw, fh, up, used, checks, credit, qa)
                extra["gate"] = {"ok": False, "failures": ["gemini QC skipped (unavailable/budget)"],
                                 "gemini_qc": "skipped (unavailable/budget)"}
                extra["layout"] = _layout(work)
                extra["scenes_picked"] = scenes_picked(work)
                extra["edit_mode"] = (qa or {}).get("edit_mode") or item.get("edit_mode")
                extra.update({"attempt": n, "ok": False, "error": "gemini QC skipped",
                              "qc_pending": True, "qc_status": "skipped",
                              "source": used, "music": credit.get("file"), "qa": checks})
                return False, extra
            g = gate.run_gate(final, qa_obj=qa, checks=checks, item=item, work=work, gemini_qc=gem_qc, out_res=OUT_RES)
            print("publish gate:", json.dumps(g, ensure_ascii=False, default=str))
            if not g["ok"] and g.get("logo_pending_only"):
                # Everything passed except a missing logo verdict (Gemini outage). Keep this render
                # (upload now) and re-check it later; never re-render a good cut for a transient error.
                extra = publish(final, fw, fh, up, used, checks, credit, qa)
                extra["gate"] = g
                extra["layout"] = _layout(work)
                extra["scenes_picked"] = scenes_picked(work)
                extra["edit_mode"] = (qa or {}).get("edit_mode") or item.get("edit_mode")
                extra.update({"attempt": n, "ok": False, "error": "final logo scan: no Gemini verdict",
                              "qc_pending": True, "logo_pending": True, "qc_status": verdict,
                              "source": used, "music": credit.get("file"), "qa": checks})
                return False, extra
            if not g["ok"]:
                last = {"attempt": n, "ok": False, "source": used, "music": credit.get("file"), "qa": checks,
                        "gate": g, "qc_status": verdict,
                        "error": "publish gate failed: " + "; ".join(g["failures"])[:600]}
                _note_source_fail(last)
                print(f"  publish gate failed; trying next official scene clip: {last['error'][:220]}", flush=True)
                continue
            extra = publish(final, fw, fh, up, used, checks, credit, qa)
            extra["gate"] = g
            extra["layout"] = _layout(work)
            extra["scenes_picked"] = scenes_picked(work)
            extra["edit_mode"] = (qa or {}).get("edit_mode") or item.get("edit_mode")
            extra["qc_status"] = "pass"
        except Exception as e:
            return False, {"attempt": n, "ok": False, "error": f"upscale/release failed: {e}",
                           "source": used, "music": credit.get("file"), "qa": checks}
        extra.update({"attempt": n, "ok": True, "error": None})
        return True, extra

RECENT_MUSIC = []
AVOID_OPENERS = []
JOB_T0 = time.monotonic()
# render.yml "Render" step timeout is 150 min. Soft budget: no new source/attempt/title is
# started past 115 min. Hard cap: at 138 min a watchdog stops the job and writes the result
# json with the exact reason (v10 Weathering looped source->title->backup until the step was
# killed at 150 min with no result).
JOB_BUDGET_S = float(os.environ.get("SKY_JOB_BUDGET_MIN", "115")) * 60.0
HARD_CAP_S = float(os.environ.get("SKY_HARD_CAP_MIN", "138")) * 60.0
MAX_SOURCES_PER_JOB = int(os.environ.get("SKY_MAX_SOURCES", "12"))
if (item or {}).get("demo") and not os.environ.get("SKY_MAX_SOURCES"):
    # Demos fail fast: the queued footage plus at most two proven swaps (~20-28 min each with
    # stabilise), so one demo job ends well inside the runner's life instead of looping 4-5
    # sources until the runner is reclaimed (demo-v16 died at 88 min on its 5th source).
    MAX_SOURCES_PER_JOB = int(os.environ.get("SKY_DEMO_MAX_SOURCES", "3"))
SOURCES_RENDERED = 0
SOURCE_EST_S = [480.0]  # running max of one source render (download+stabilize+cut+QA)
LAST_INFO = {}
ATTEMPTS_LOG_REF = []


class JobTimeout(Exception):
    pass


def _on_hard_cap(signum, frame):
    raise JobTimeout(f"hard time cap {HARD_CAP_S/60:.0f} min reached")


def _budget_left_for(next_est_s: float) -> bool:
    used = time.monotonic() - JOB_T0
    ok = used + next_est_s * 1.1 <= JOB_BUDGET_S
    if not ok:
        print(f"::warning::time budget: {used/60:.1f} min used, next attempt ~{next_est_s/60:.1f} min "
              f"would pass the {JOB_BUDGET_S/60:.0f} min budget; skipping further attempts", flush=True)
    return ok


RESULT_KEYS = (
    "release_url", "asset_url", "cover_url", "qa", "drop_t", "energy", "source",
    "music", "music_credit", "description_final", "gate", "no_publish",
    "scenes_picked", "qc_status", "layout",
)


def _layout(work):
    try:
        return json.loads((Path(work) / "layout.json").read_text())
    except Exception:
        return None


def _result_extra(info):
    return {k: v for k, v in info.items() if k in RESULT_KEYS}


def _record_attempt(attempts_log, info):
    attempts_log.append({k: info.get(k) for k in ("attempt", "ok", "error", "source", "music", "qc_status")})


def _write_qc_pending(info, attempts_log, auto_retries):
    """Render exists but Gemini QC was skipped — never mark done/ready."""
    why = info.get("error") or "gemini QC skipped"
    fields = dict(_result_extra(info))
    fields.update(auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
                  attempts=attempts_log, last_error=why, qc_pending=True,
                  qc_status=info.get("qc_status") or "skipped",
                  logo_pending=bool(info.get("logo_pending")))
    write("retry_queued", f"{why}; render kept, will re-check with the next key (no re-render)", **fields)
    print(f"::warning::{iid}: {why}; queued re-check with next Gemini key")
    return 0



def _refresh_item_context():
    """Rewrite SKY_ITEM_JSON and queue_meta after a same-run title swap."""
    global QUEUE_META
    QUEUE_META = {k: item.get(k) for k in ("hook", "title", "description", "tags")}
    ctx = OUT / f"item_{iid}.json"
    ctx.write_text(json.dumps({"item": item, "avoid_openers": AVOID_OPENERS,
                               "trends": gemini.load_trends(REPO)}, ensure_ascii=False))
    os.environ["SKY_ITEM_JSON"] = str(ctx)
    # The hook / scene-pick caches describe the OLD footage. demo-v16 swapped Oppenheimer ->
    # Mononoke and reused the cached "Zero Is Never Zero" hook, so Gemini QC rejected a clean
    # render for "title says Oppenheimer, footage is Mononoke".
    drop_footage_caches()


def drop_footage_caches():
    for name in (f"gemini_hook_{iid}.json", f"gemini_scenes_{iid}.json"):
        try:
            (OUT / name).unlink(missing_ok=True)
        except Exception:
            pass


def _try_title_swap(reason, tried) -> bool:
    """Replace the current topic with the next official same-format title."""
    global item
    if not title_swap.should_swap(reason):
        return False
    nxt = title_swap.next_eligible_title(item, exclude=tried, root=REPO)
    if not nxt:
        print(
            f"  title auto-swap: no other official {title_swap.item_format(item)} title left",
            flush=True,
        )
        return False
    before = title_swap.title_slug(item)
    title_swap.apply_title_swap(item, nxt, reason=str(reason or "")[:400])
    tried |= title_swap.item_keys(item)
    _refresh_item_context()
    print(
        f"  title auto-swap: {before} -> {title_swap.title_slug(item)} "
        f"({item.get('words')}) format={title_swap.item_format(item)}",
        flush=True,
    )
    return True


def _try_backup(reason, tried) -> bool:
    """Last rung: any remaining official backup/pool title."""
    global item
    nxt = title_swap.next_fallback_item(item, exclude=tried, root=REPO, same_format_only=False)
    if not nxt:
        print("  backup fallthrough: no official backup/pool title left", flush=True)
        return False
    before = title_swap.title_slug(item)
    title_swap.apply_title_swap(item, nxt, reason=str(reason or "")[:400])
    tried |= title_swap.item_keys(item)
    _refresh_item_context()
    print(
        f"  backup fallthrough: {before} -> {title_swap.title_slug(item)} "
        f"({item.get('words')}) format={title_swap.item_format(item)}",
        flush=True,
    )
    return True


def main():
    prev = read_prev()
    auto_retries = int(prev.get("auto_retries") or 0)
    # This job IS the outer auto-retry if the last result asked for one.
    if prev.get("status") == "retry_queued":
        auto_retries += 1
    prior_music = []
    if prev.get("music"):
        prior_music.append(prev["music"])
    for a in prev.get("attempts") or []:
        if a.get("music"):
            prior_music.append(a["music"])

    fetch_music_library()
    recent = recent_music()
    if recent:
        print("music: skipping songs used in the last 3 days:", recent)
    # Hook bar/title are written inside the pipeline from the chosen clip (sky_ai.clip_hook_meta).
    global AVOID_OPENERS
    AVOID_OPENERS = sky_ai.recent_openers(REPO, iid)
    ctx = OUT / f"item_{iid}.json"
    ctx.write_text(json.dumps({"item": item, "avoid_openers": AVOID_OPENERS,
                               "trends": gemini.load_trends(REPO)}, ensure_ascii=False))
    os.environ["SKY_ITEM_JSON"] = str(ctx)
    global RECENT_MUSIC
    RECENT_MUSIC = recent

    rc = None
    if prev.get("qc_pending") and prev.get("asset_url"):
        rc = _qc_only_retry(prev, auto_retries)
        if rc in (0, 1):
            return rc
    if rc == "skipped":
        print("  QC-only retry still skipped; leaving item queued", flush=True)
        if auto_retries < MAX_AUTO_RETRIES:
            write("retry_queued", "gemini QC still skipped; will retry QC with the next key",
                  auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
                  attempts=list(prev.get("attempts") or []) + [{"attempt": "qc_retry", "ok": False,
                                                                "error": "gemini QC skipped"}],
                  last_error="gemini QC skipped", qc_pending=True, qc_status="skipped",
                  **{k: prev[k] for k in RESULT_KEYS if k in prev and k != "qc_status"})
            return 0
        write("failed", "gemini QC skipped after retries; not marking ready",
              auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
              attempts=prev.get("attempts") or [], last_error="gemini QC skipped",
              qc_status="skipped", **{k: prev[k] for k in RESULT_KEYS if k in prev and k != "qc_status"})
        return 1

    attempts_log = ATTEMPTS_LOG_REF
    tried = title_swap.item_keys(item)
    swaps = 0
    backups = 0
    while True:
        # Attempt 1: normal (all official clips of this title)
        t_a = time.monotonic()
        ok, info = attempt(1, force_fallback=False)
        est = time.monotonic() - t_a
        _record_attempt(attempts_log, info)
        if info.get("qc_pending"):
            return _write_qc_pending(info, attempts_log, auto_retries)
        if info.get("capped"):
            return _give_up(attempts_log, auto_retries, last_info=info)
        if ok:
            detail = info["detail"]
            if item.get("title_swaps"):
                detail += f" (title auto-swap to {item.get('words') or title_swap.title_slug(item)})"
            write("done", detail, auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
                  attempts=attempts_log, last_error=None, **_result_extra(info))
            return 0

        def _swap_or_give_up(current):
            nonlocal swaps, backups
            err = (current or {}).get("error") or (attempts_log[-1].get("error") if attempts_log else "")
            # Do not start another long render if the job budget is already gone.
            if not _budget_left_for(min(est, SOURCE_EST_S[0] * 2)) or SOURCES_RENDERED >= MAX_SOURCES_PER_JOB:
                return "give_up"
            action = title_swap.recovery_action(
                err, sources_left=False,
                titles_left=swaps < title_swap.MAX_SWAPS,
                backups_left=backups < title_swap.MAX_SWAPS,
            )
            if action == title_swap.NEXT_TITLE and _try_title_swap(err, tried):
                swaps += 1
                return "swap"
            if action in (title_swap.NEXT_TITLE, title_swap.BACKUP) and _try_backup(err, tried):
                backups += 1
                return "swap"
            return "give_up"

        # Attempt 2: sources-release fallback mp4
        if (_qc_rejects(attempts_log) >= 2 or not _budget_left_for(est)
                or sourcing.is_unrecoverable_source_error(info.get("error"))
                or title_swap.should_swap(info.get("error"))):
            if _swap_or_give_up(info) == "swap":
                continue
            return _give_up(attempts_log, auto_retries, last_info=info)
        t_a = time.monotonic()
        ok, info = attempt(2, force_fallback=True)
        est = max(est, time.monotonic() - t_a)
        _record_attempt(attempts_log, info)
        if info.get("qc_pending"):
            return _write_qc_pending(info, attempts_log, auto_retries)
        if info.get("capped"):
            return _give_up(attempts_log, auto_retries, last_info=info)
        if ok:
            write("done", info["detail"] + " (retry: sources fallback)", auto_retries=auto_retries,
                  max_auto_retries=MAX_AUTO_RETRIES, attempts=attempts_log, last_error=None,
                  **_result_extra(info))
            return 0

        # Attempt 3: fallback + different music + alt scenes + relaxed non-hard thresholds
        if (_qc_rejects(attempts_log) >= 2 or not _budget_left_for(est)
                or title_swap.should_swap(info.get("error"))):
            if _swap_or_give_up(info) == "swap":
                continue
            return _give_up(attempts_log, auto_retries, last_info=info)
        excl = list(dict.fromkeys([m for m in prior_music + [attempts_log[0].get("music"), attempts_log[-1].get("music")] if m]))
        ok, info = attempt(3, force_fallback=True, relax=True, alt_scenes=True,
                           exclude_music=excl, music_salt=f"retry3-{auto_retries}-{swaps}")
        _record_attempt(attempts_log, info)
        if info.get("qc_pending"):
            return _write_qc_pending(info, attempts_log, auto_retries)
        if info.get("capped"):
            return _give_up(attempts_log, auto_retries, last_info=info)
        if ok:
            write("done", info["detail"] + " (retry: alt music/scenes, non-hard relax)", auto_retries=auto_retries,
                  max_auto_retries=MAX_AUTO_RETRIES, attempts=attempts_log, last_error=None,
                  **_result_extra(info))
            return 0

        if _swap_or_give_up(info) == "swap":
            continue
        return _give_up(attempts_log, auto_retries, last_info=info)


def _qc_rejects(attempts_log) -> int:
    return sum(1 for a in attempts_log if "gemini QC reject" in str(a.get("error") or ""))


def _give_up(attempts_log, auto_retries, last_info=None):
    last_info = last_info or {}
    if last_info.get("qc_pending"):
        return _write_qc_pending(last_info, attempts_log, auto_retries)
    last = attempts_log[-1].get("error") or "unknown failure"
    # Gemini QC watched the final cut and rejected it twice: do not burn more renders; mark failed so
    # plan.py's backup rule renders a backup item for this slot (never a skipped slot).
    if _qc_rejects(attempts_log) >= 2:
        write("failed", ("gemini QC rejected 2 re-renders; backup item takes the slot: " + last)[:800],
              auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES, attempts=attempts_log, last_error=last,
              qc_rejected=True)
        print(f"::error::{iid}: Gemini QC rejected 2 renders; backup takes the slot")
        return 1
    # Missing/rejected sources will not appear on a retry of the same topic.
    # Mark failed now so plan.py can assign the next backup instead of leaving the slot empty.
    if sourcing.is_unrecoverable_source_error(last):
        write("failed", ("no usable official source left; next backup takes the slot: " + last)[:800],
              auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
              attempts=attempts_log, last_error=last, source_unrecoverable=True)
        print(f"::error::{iid}: unrecoverable source; backup takes the slot")
        return 1
    # All 3 in-job attempts failed. If we still have an outer auto-retry, queue it
    # and exit 0 so this run does not wake BIBI yet.
    if auto_retries < MAX_AUTO_RETRIES:
        write("retry_queued",
              f"all {len(attempts_log)} in-job attempt(s) failed; auto-retry {auto_retries + 1}/{MAX_AUTO_RETRIES} will be dispatched: {last}"[:800],
              auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
              attempts=attempts_log, last_error=last)
        print(f"::warning::{iid}: queued outer auto-retry ({auto_retries + 1}/{MAX_AUTO_RETRIES})")
        return 0

    write("failed", last[:800], auto_retries=auto_retries, max_auto_retries=MAX_AUTO_RETRIES,
          attempts=attempts_log, last_error=last)
    print(f"::error::{iid}: {last[:300]}")
    return 1

def _crash_result(reason):
    """Always leave a result json with the exact reason (hard cap, crash) before the job dies."""
    try:
        log = list(ATTEMPTS_LOG_REF) or [{"attempt": 0, "ok": False, "error": reason}]
        prev = read_prev()
        auto = int(prev.get("auto_retries") or 0) + (1 if prev.get("status") == "retry_queued" else 0)
        last = str(log[-1].get("error") or "")
        detail = f"{reason}" + (f"; last attempt: {last}" if last and last != reason else "")
        if auto < MAX_AUTO_RETRIES:
            write("retry_queued", ("render stopped: " + detail)[:800], auto_retries=auto,
                  max_auto_retries=MAX_AUTO_RETRIES, attempts=log, last_error=detail[:800])
            print(f"::warning::{iid}: {detail[:300]} (auto-retry queued)")
            return 0
        write("failed", ("render stopped: " + detail)[:800], auto_retries=auto,
              max_auto_retries=MAX_AUTO_RETRIES, attempts=log, last_error=detail[:800])
        print(f"::error::{iid}: {detail[:300]}")
        return 1
    except Exception as e:  # never mask the original failure
        print(f"::error::{iid}: could not write result json: {e}")
        return 1


if __name__ == "__main__":
    import signal
    import traceback
    signal.signal(signal.SIGALRM, _on_hard_cap)
    signal.alarm(max(60, int(HARD_CAP_S - (time.monotonic() - JOB_T0))))
    try:
        rc = main()
    except JobTimeout as e:
        rc = _crash_result(str(e))
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        rc = _crash_result(f"render_item crashed: {type(e).__name__}: {e}"[:400])
    finally:
        signal.alarm(0)
    sys.exit(rc)
