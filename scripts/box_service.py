#!/usr/bin/env python3
"""Skywalker box downloader service (same design as pro-edit's box_fetch).

Why: GitHub runners hit YouTube's "confirm you're not a bot" wall. The BOX downloads; GitHub edits.
Flow (fully automatic, no assistant in the loop):
  1. GitHub / Gemini decide what is needed and write it to the repo:
       - queue/<date>.json items + config/backup_pool.json   -> the scene clips each item needs
       - links/requests.json (pending rows)                  -> link mode (an owner / Gemini link)
       - config/clip_requests.json (clips.yml, Gemini)       -> new official scene clips per title
       - music/library.json                                   -> the songs the edits may use
  2. This service (scripts/box_service.sh loop + watchdog) polls the repo every few minutes, downloads
     with yt-dlp on the box, vets each clip, uploads it to the `sources` / `music` release under the exact
     asset name the queue expects, writes `box_manifest.json` + `box_heartbeat.json` to the release and
     marks the request done (config/clip_fetch_status.json, links/requests.json).
  3. GitHub Actions (render.yml) pulls those release assets and edits.

Source rules (Skywalker): SCENE clips only, never trailers/teasers; link mode first; official X before
YouTube; then other official hosts; >=1080p; skip watermarked / logo-burn channels (no crop, no blur, no
fan re-uploads); no piracy, no DRM / paid OTT rips. Every new clip is checked by overlay_guard and, when
Gemini keys are present, Gemini looks at keyframes (logo / watermark / burned subtitles / HUD / quality /
trailer card) exactly like pro-edit's vet().

Auth: SKY_YTDLP_BROWSER (e.g. chrome:/home/box/chrome-profile/Fork-2) lets yt-dlp read the box browser's
own YouTube / X login for downloads only. Cookies are never copied, committed, uploaded or logged.
Idempotent: every step compares the release asset list with what is needed; restart-safe (flock).

  python3 scripts/box_service.py cycle        # one full pass (what the loop runs)
  python3 scripts/box_service.py sync         # fast-forward the box clone to origin/main (compile-checked)
  python3 scripts/box_service.py status       # heartbeat + what is still missing
  python3 scripts/box_service.py clips|links|requests|music|dispatch   # single steps
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

GH_REPO = os.environ.get("SKY_GH_REPO", "Bhupi820970/skywalker-shorts")
HOME = Path(os.environ.get("SKY_BOX_HOME", "/workspace/skywalker-box"))
STATE = HOME / "state.json"
LOGF = HOME / "box_service.log"
WORK = HOME / "work"
MUSIC_CACHE = [Path(p) for p in os.environ.get("SKY_MUSIC_CACHE", str(HOME / "music_cache")).split(":") if p]
IST = timezone(timedelta(hours=5, minutes=30))
TRAILER = re.compile(r"\b(trailer|teaser|promo|tv[ _-]?spot)\b", re.I)
ASSET_RX = re.compile(r"^([a-z0-9]+)_scene(\d+)_h264\.mp4$")
URL_OK = re.compile(r"^https://(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/)[\w-]{11}|youtu\.be/[\w-]{11}|"
                    r"(x|twitter)\.com/\w{1,30}/status/\d{5,25}|www\.dailymotion\.com/video/\w+|"
                    r"www\.bilibili\.com/video/\w+|archive\.org/(details|download)/[\w.\-/%]+)")
MAX_TRIES_PER_DAY = 3
GIT_ID = ["-c", "user.name=skywalker-box", "-c", "user.email=bot@users.noreply.github.com"]
# workflow: (max age minutes, earliest IST hh:mm or None) - backup for late GitHub cron (old box timer)
JOBS = {
    "health.yml": (28, None),
    "render.yml": (32, None),
    "publish.yml": (32, None),
    "stats.yml": (23 * 60, "08:45"),
    "trends.yml": (71 * 60, "06:30"),
    "clips.yml": (23 * 60, "09:15"),
}
_LAST_DISPATCH = [0.0]


# ---------------------------------------------------------------- utils
def log(msg: str) -> None:
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST box: {msg}"
    print(line, flush=True)


def run(cmd, timeout=600, cwd=None, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd or REPO, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def gh(*args, timeout=300):
    env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1")
    return run(["gh", *args], timeout=timeout, env=env)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    tmp.replace(STATE)


def today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def due(st: dict, key: str, every_s: float) -> bool:
    return time.time() - float((st.get("last") or {}).get(key) or 0) >= every_s


def mark(st: dict, key: str) -> None:
    st.setdefault("last", {})[key] = time.time()
    save_state(st)


def tries_ok(st: dict, key: str) -> bool:
    t = (st.get("tries") or {}).get(key) or {}
    return not (t.get("day") == today() and int(t.get("n") or 0) >= MAX_TRIES_PER_DAY)


def count_try(st: dict, key: str, err: str = "") -> None:
    tr = st.setdefault("tries", {})
    t = tr.get(key) or {}
    if t.get("day") != today():
        t = {"day": today(), "n": 0}
    t["n"] = int(t.get("n") or 0) + 1
    t["err"] = err[:200]
    tr[key] = t
    save_state(st)


def browser_args() -> list[str]:
    b = (os.environ.get("SKY_YTDLP_BROWSER") or "").strip()
    if not b or b.startswith("-") or any(ch in b for ch in ";|&`$\n"):
        return []
    return ["--cookies-from-browser", b]


def js_runtime_args() -> list[str]:
    """Box only: SKY_YTDLP_JS_RUNTIME (e.g. deno:/home/box/.deno/bin/deno) so yt-dlp can
    solve YouTube n-challenges and select 1080p+ formats. Safe no-op when unset."""
    r = (os.environ.get("SKY_YTDLP_JS_RUNTIME") or "").strip()
    if not r:
        p = Path("/home/box/.deno/bin/deno")
        if p.is_file() and os.access(p, os.X_OK):
            r = f"deno:{p}"
    if not r or r.startswith("-") or any(ch in r for ch in ";|&`$\n"):
        return []
    return ["--js-runtimes", r]


def release_assets(tag: str) -> dict[str, int]:
    r = gh("api", "--paginate", f"repos/{GH_REPO}/releases/tags/{tag}", "-q", ".assets[] | [.name, .size] | @tsv",
           timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"cannot list release {tag}: {(r.stderr or '')[-160:]}")
    out = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            n, s = line.split("\t", 1)
            out[n] = int(s or 0)
    return out


def upload(tag: str, path: Path) -> bool:
    for i in range(3):
        r = gh("release", "upload", tag, str(path), "-R", GH_REPO, "--clobber", timeout=3600)
        if r.returncode == 0:
            return True
        log(f"upload {path.name} -> {tag} failed (try {i + 1}): {(r.stderr or '')[-160:]}")
        time.sleep(10 * (i + 1))
    return False


# ---------------------------------------------------------------- git (box clone)
def git(*a, timeout=180):
    return run(["git", *a], timeout=timeout)


def sync_code() -> str:
    """Fast-forward the box clone to origin/main; keep the old code if the new one does not compile."""
    if git("fetch", "-q", "origin", "main").returncode != 0:
        return "fetch failed (offline?)"
    old = git("rev-parse", "HEAD").stdout.strip()
    new = git("rev-parse", "origin/main").stdout.strip()
    if old == new:
        return "up to date"
    git("reset", "-q", "--hard", "origin/main")
    files = ["scripts/box_service.py", "scripts/clip_fetch.py", "sourcing.py", "music_lib.py",
             "overlay_guard.py", "scripts/ingest_music.py", "scripts/submit_link.py", "gemini.py",
             "posted_history.py", "title_swap.py"]
    bad = [f for f in files if (REPO / f).exists() and run([sys.executable, "-m", "py_compile", f]).returncode != 0]
    if bad:
        git("reset", "-q", "--hard", old)
        return f"new code does not compile ({', '.join(bad)}); staying on {old[:8]}"
    return f"updated {old[:8]} -> {new[:8]}"


def commit_push(paths: list[str], msg: str) -> bool:
    git("add", *paths)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return True
    if git(*GIT_ID, "commit", "-q", "-m", msg).returncode != 0:
        return False
    for i in range(5):
        if git("pull", "--rebase", "-q", "origin", "main").returncode != 0:
            git("rebase", "--abort")
            git("reset", "-q", "--hard", "origin/main")
            log(f"commit '{msg}': rebase conflict; dropped (next cycle recomputes)")
            return False
        if git("push", "-q", "origin", "HEAD:main").returncode == 0:
            log(f"committed: {msg}")
            return True
        time.sleep(5 * (i + 1))
    git("reset", "-q", "--hard", "origin/main")
    return False


# ---------------------------------------------------------------- vetting
def probe(p: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,codec_name:format=duration,size", "-of", "json", str(p)], timeout=60)
    try:
        d = json.loads(r.stdout or "{}")
    except Exception:
        d = {}
    s = (d.get("streams") or [{}])[0]
    f = d.get("format") or {}
    return {"w": int(s.get("width") or 0), "h": int(s.get("height") or 0), "codec": s.get("codec_name"),
            "dur": float(f.get("duration") or 0)}


def url_meta(url: str) -> dict | None:
    r = run(["yt-dlp", "-J", "--no-warnings", "--no-playlist", "--skip-download", "--remote-components",
             "ejs:github", *js_runtime_args(), *browser_args(), url], timeout=120)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def pre_check(url: str, meta: dict | None) -> str | None:
    """Cheap checks before downloading: trailer, logo-burn channel, max height, length."""
    import sourcing
    try:
        import overlay_guard
    except Exception:
        overlay_guard = None
    if not meta:
        return "no metadata (private / removed / blocked)"
    title = meta.get("title") or ""
    ch = meta.get("channel") or meta.get("uploader") or ""
    if TRAILER.search(title) or sourcing.is_trailer_source(title) or sourcing.is_trailer_source(url):
        return f"trailer/teaser: {title[:60]}"
    if sourcing.is_logo_burn_channel(ch) or (overlay_guard and overlay_guard.channel_blocked(ch)):
        return f"logo/subtitle-burn channel: {ch}"
    hs = [int(f.get("height") or 0) for f in meta.get("formats") or [] if f.get("vcodec") not in (None, "none")]
    if hs and max(hs) < 1080:
        return f"best is {max(hs)}p (< 1080p)"
    dur = float(meta.get("duration") or 0)
    if dur and not (12 <= dur <= 900):
        return f"length {dur:.0f}s outside 12-900s"
    if meta.get("is_live") or meta.get("availability") in ("premium_only", "subscriber_only", "needs_auth"):
        return "live / paid / members-only"
    return None


def official_reason(meta: dict | None, item: dict, slug: str) -> str | None:
    """Official studio / distributor channels only (never fan re-upload channels)."""
    if not meta:
        return None
    try:
        import clip_fetch
        cat = json.loads((REPO / "config" / "source_catalog.json").read_text()).get("topics") or {}
        chans = clip_fetch.official_set({"official_channels": list(item.get("official_channels") or [])
                                         + list((cat.get(slug) or {}).get("official_channels") or [])})
        if clip_fetch.is_official(meta, chans):
            return None
    except Exception as e:
        return f"official check failed ({type(e).__name__})"
    return f"not an official channel: {meta.get('channel') or meta.get('uploader')}"


def download_clip(url: str, dest: Path) -> bool:
    for f in dest.parent.glob(dest.stem + "*"):
        f.unlink(missing_ok=True)
    fmt = "bv*[height>=1080][height<=2160]+ba/b[height>=1080][height<=2160]"
    r = run(["yt-dlp", "--no-playlist", "--remote-components", "ejs:github", "-f", fmt,
             "-S", "res:2160,vcodec:h264,vcodec:vp9,acodec:m4a", "--merge-output-format", "mp4",
             "-o", str(dest), "--no-warnings", "--no-part", "--retries", "3", *js_runtime_args(), *browser_args(), url], timeout=1800)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 1_000_000


def gemini_vet(path: Path, title: str, channel: str) -> str | None:
    """pro-edit style: Gemini looks at 6 keyframes and flags logo / watermark / burned subs / HUD / cards."""
    try:
        import gemini
    except Exception:
        return None
    if not gemini.available():
        return None
    m = probe(path)
    dur = m["dur"] or 0
    if dur <= 0:
        return None
    kdir = WORK / "keyframes"
    kdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(6):
        ts = dur * (0.06 + 0.86 * i / 5)
        fp = kdir / f"{path.stem}_k{i}.jpg"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", "scale=960:-2", "-q:v", "3", str(fp)], timeout=60)
        if fp.exists() and fp.stat().st_size > 400:
            frames.append(fp)
    if len(frames) < 4:
        return None
    prompt = (
        f"You vet source footage for a YouTube Shorts edit channel. {len(frames)} keyframes from one clip "
        f"(title: {title[:100]!r}, channel: {channel[:60]!r}). Reply JSON only: "
        '{"watermark": bool, "channel_logo": bool, "burned_subtitles": bool, "hud_overlay": bool, '
        '"trailer_card": bool, "official_quality": bool, "editable": bool, "reasons": [str]}. '
        "watermark/channel_logo = any streamer/studio/channel bug or logo burned into the picture (a tiny original "
        "studio corner mark that is part of the film frame is NOT a channel logo); burned_subtitles = hardcoded "
        "subtitles; hud_overlay = game HUD/UI or social-media frame; trailer_card = title/date/rating/coming-soon "
        "cards dominate; official_quality = sharp official master, not a cam/re-encode; editable = real story or "
        "character footage usable for a beat-cut edit."
    )
    try:
        res = gemini.generate_json(prompt, images=[str(f) for f in frames], purpose="logo-vet", temperature=0.1)
    finally:
        for f in frames:
            f.unlink(missing_ok=True)
    if not isinstance(res, dict):
        return None
    # same fatal flags as pro-edit's vet(); a stray end/title card is cut out later by the render's card filter
    flags = [k for k in ("watermark", "channel_logo", "burned_subtitles", "hud_overlay") if res.get(k)]
    if res.get("official_quality") is False:
        flags.append("not official quality")
    if res.get("editable") is False:
        flags.append("not editable")
    return ("gemini: " + ", ".join(flags)) if flags else None


def vet_file(path: Path, title: str, channel: str) -> str | None:
    m = probe(path)
    if min(m["w"], m["h"]) < 1080:
        return f"{m['w']}x{m['h']} below 1080p"
    if m["dur"] < 12:
        return f"too short {m['dur']:.0f}s"
    try:
        import overlay_guard
        why = overlay_guard.clip_reason(path)
        if why:
            return f"overlay_guard: {why}"
    except Exception as e:
        log(f"overlay_guard unavailable ({type(e).__name__}); relying on Gemini + render QC")
    return gemini_vet(path, title, channel)


# ---------------------------------------------------------------- needs (what the repo asks for)
def _asset_of(u: str) -> str:
    return u.rsplit("/", 1)[-1] if "/releases/download/sources/" in (u or "") else ""


def _slot_too_late(it: dict, hours: float = 6.0) -> bool:
    raw = str(it.get("slot") or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=IST)
    return dt < datetime.now(IST) - timedelta(hours=hours)


def scene_requests() -> list[dict]:
    """config/scene_requests.json (GitHub's plan: show, scenes, queries, missing assets), soonest slot first."""
    try:
        rows = json.loads((REPO / "config" / "scene_requests.json").read_text()).get("requests") or []
    except Exception:
        return []
    return sorted([r for r in rows if r.get("slug")], key=lambda r: (str(r.get("slot") or "9999"), r["slug"]))


def needed_clips(days: int = 8) -> list[dict]:
    """Scene assets the queue (today..+days, soonest slot first) and the backup pool point at."""
    out: dict[str, dict] = {}
    start = datetime.now(IST).date()
    for i in range(days):
        f = REPO / "queue" / f"{start + timedelta(days=i):%Y-%m-%d}.json"
        try:
            items = json.loads(f.read_text())
        except Exception:
            continue
        for it in items:
            if (it.get("status") or "queued") not in ("queued", "retry_queued"):
                continue
            if _slot_too_late(it):
                continue  # publish skips slots >6h late; spend the box on slots that can still post
            for u in (it.get("fallback_mp4") or []) + (it.get("sources") or []):
                a = _asset_of(u)
                if a and ASSET_RX.match(a) and not TRAILER.search(a):
                    out.setdefault(a, {"asset": a, "slot": it.get("slot") or "", "item": it, "why": "queue"})
    try:
        pool = json.loads((REPO / "config" / "backup_pool.json").read_text()).get("items") or []
    except Exception:
        pool = []
    for it in pool:
        for u in (it.get("fallback_mp4") or []) + (it.get("sources") or []):
            a = _asset_of(u)
            if a and ASSET_RX.match(a) and not TRAILER.search(a):
                out.setdefault(a, {"asset": a, "slot": "9999", "item": it, "why": "backup_pool"})
    return sorted(out.values(), key=lambda r: (r["slot"], r["asset"]))


def url_index() -> dict[str, str]:
    """asset -> original official URL: clip_fetch history (all of git), then the catalog scene_urls."""
    idx: dict[str, str] = {}
    try:
        cat = json.loads((REPO / "config" / "source_catalog.json").read_text()).get("topics") or {}
    except Exception:
        cat = {}
    for slug, row in cat.items():
        for i, u in enumerate(row.get("scene_urls") or [], 1):
            idx.setdefault(f"{slug}_scene{i}_h264.mp4", u)
    shas = run(["git", "log", "--format=%H", "-n", "60", "--", "config/clip_fetch_status.json"]).stdout.split()
    for sha in ["HEAD"] + shas:
        r = run(["git", "show", f"{sha}:config/clip_fetch_status.json"])
        try:
            clips = json.loads(r.stdout).get("clips") or []
        except Exception:
            continue
        for c in clips:
            cid = str(c.get("id") or "")
            if cid.startswith("yt:"):
                idx[c["asset"]] = f"https://www.youtube.com/watch?v={cid[3:]}"  # history wins over catalog
    return idx


def resolve_url(need: dict, idx: dict[str, str]) -> str:
    a = need["asset"]
    u = idx.get(a) or ""
    m = ASSET_RX.match(a)
    if not u and m and m.group(2) == "1":
        u = need["item"].get("source_url") or ""
    if u and (TRAILER.search(u) or not URL_OK.match(u)):
        return ""
    return u


# ---------------------------------------------------------------- steps
def step_links(st: dict) -> int:
    """Link mode first: pending rows in links/requests.json -> queue items (the clip step downloads them)."""
    try:
        import submit_link
        pend = submit_link.pending_requests(REPO)
    except Exception as e:
        log(f"links: skipped ({type(e).__name__}: {e})")
        return 0
    if not pend:
        return 0
    good = 0
    for row in pend:
        url = submit_link.normalize_url(row.get("url") or "")
        if not submit_link.is_supported_link(url) or submit_link.link_is_trailer(url, row.get("title") or ""):
            submit_link.record_request(url, {"title": row.get("title")}, status="rejected",
                                       detail="not an official YouTube/X scene link (trailers are never used)", root=REPO)
            continue
        args = ["--url", url]
        for k in ("title", "hook", "name", "slot", "words"):
            if row.get(k):
                args += [f"--{k}", str(row[k])]
        r = run([sys.executable, "scripts/submit_link.py", *args], timeout=120)
        if r.returncode == 0:
            good += 1
        else:
            log(f"links: submit failed for a row: {(r.stderr or r.stdout)[-160:]}")
    commit_push(["queue", "links/requests.json"], f"box links: queued {good} official link(s)")
    return good


def step_clips(st: dict, tick=lambda: None, limit: int = 0) -> dict:
    """Restore / fetch every scene clip the queue + backup pool need that is not on the sources release."""
    have = release_assets("sources")
    needs = [n for n in needed_clips() if n["asset"] not in have]
    idx = url_index()
    done, failed, search_slugs = [], [], {}
    WORK.mkdir(parents=True, exist_ok=True)
    reqs = scene_requests()
    log(f"clips: {len(needs)} needed scene clip(s) missing on the release; "
        f"{len(reqs)} scene request(s) ({sum(1 for r in reqs if r.get('for') == 'slot')} for upcoming slots)")
    # Upcoming slots first: their exact assets, then an official search for any slot title that still
    # has <2 clips on the release. The backup-pool backlog comes after.
    slot_needs = [n for n in needs if n["why"] == "queue"]
    pool_needs = [n for n in needs if n["why"] != "queue"]
    req_by_slug = {r["slug"]: r for r in reqs}
    slot_search = {}
    for r in reqs:
        if r.get("for") == "slot" and len(r.get("on_release") or []) < 2:
            slot_search.setdefault(r["slug"], r)

    def _search_now(slugs: dict) -> int:
        if not slugs or limit:
            return 0
        rows = {k: dict(req_by_slug.get(k) or {}, **{kk: vv for kk, vv in v.items() if vv}) for k, v in slugs.items()}
        return search_fill(st, rows, tick)

    searched = 0
    for phase, plist in (("slot", slot_needs), ("pool", pool_needs)):
      for n in plist:
          if limit and len(done) >= limit:
              break
          tick()
          a = n["asset"]
          url = resolve_url(n, idx)
          if not url:
              search_slugs.setdefault(ASSET_RX.match(a).group(1), n["item"])
              continue
          if not tries_ok(st, a):
              continue
          meta = url_meta(url)
          why = pre_check(url, meta) or official_reason(meta, n["item"], ASSET_RX.match(a).group(1))
          if why:
              log(f"clips: {a}: skip {url} ({why})")
              count_try(st, a, why)
              st.setdefault("bad_urls", {})[url] = why
              search_slugs.setdefault(ASSET_RX.match(a).group(1), n["item"])
              failed.append(a)
              continue
          dest = WORK / a
          try:
              if not download_clip(url, dest):
                  count_try(st, a, "download failed")
                  failed.append(a)
                  continue
              why = vet_file(dest, meta.get("title") or "", meta.get("channel") or meta.get("uploader") or "")
              if why:
                  log(f"clips: {a}: reject ({why}); skipped, never cropped")
                  count_try(st, a, why)
                  st.setdefault("bad_urls", {})[url] = why
                  search_slugs.setdefault(ASSET_RX.match(a).group(1), n["item"])
                  failed.append(a)
                  continue
              if upload("sources", dest):
                  m = probe(dest)
                  rec = {"asset": a, "id": "yt:" + meta["id"] if "youtube" in url or "youtu.be" in url else meta.get("id"),
                         "url": url, "title": (meta.get("title") or "")[:120],
                         "channel": meta.get("channel") or meta.get("uploader") or "", "kind": "scene",
                         "res": f"{m['w']}x{m['h']}", "for": n["why"], "by": "box",
                         "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                  st.setdefault("uploaded", []).append(rec)
                  st["uploaded"] = st["uploaded"][-1000:]
                  save_state(st)
                  done.append(rec)
                  log(f"clips: uploaded {a} {rec['res']} [{n['why']}] {rec['channel']}: {rec['title'][:50]}")
              else:
                  count_try(st, a, "upload failed")
                  failed.append(a)
          finally:
              for f in WORK.glob(Path(a).stem + "*"):
                  f.unlink(missing_ok=True)
      if phase == "slot":
          slot_search.update({k: v for k, v in search_slugs.items()})
          searched += _search_now(slot_search)
          for k in slot_search:
              search_slugs.pop(k, None)

    # titles with no usable original URL: official search (X first, then YouTube / DM / Bilibili) via clip_fetch
    searched += _search_now(search_slugs)
    if done:
        record_status(done)
    st["clips_summary"] = {"at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "needed_missing": len(needs),
                           "uploaded_now": len(done), "failed_now": len(failed), "search_titles": len(search_slugs),
                           "search_uploaded": searched}
    save_state(st)
    return st["clips_summary"]


def search_fill(st: dict, slugs: dict, tick) -> int:
    """Run clip_fetch (official channel search, X first) for titles whose original clip is gone."""
    try:
        cat = json.loads((REPO / "config" / "source_catalog.json").read_text()).get("topics") or {}
    except Exception:
        cat = {}
    reqs = []
    for slug, it in slugs.items():
        if not tries_ok(st, f"search:{slug}"):
            continue
        words = (it.get("words") or slug).strip()
        reqs.append({"slug": slug, "pillar": it.get("pillar") or "anime", "words": words,
                     "scene_types": it.get("scene_types") or ["fight", "emotional", "aura"],
                     "search_queries": it.get("search_queries") or [
                         f"{words} fight scene official clip", f"{words} best scene official clip",
                         f"{words} official clip"],
                     "official_channels": (cat.get(slug) or {}).get("official_channels")
                     or it.get("official_channels") or [],
                     "hook": it.get("hook") or "", "title": it.get("title") or "", "tags": it.get("tags") or [],
                     "market": it.get("market") or "US/UK"})
        count_try(st, f"search:{slug}", "search")
    if not reqs:
        return 0
    f = HOME / "box_clip_requests.json"
    f.write_text(json.dumps({"generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
                             "max_clips_per_title": 3, "requests": reqs}, indent=1))
    before = len(release_assets("sources"))
    env = dict(os.environ, CLIP_REQUESTS_FILE=str(f), CLIP_WORK=str(WORK / "search"))
    r = run([sys.executable, "scripts/clip_fetch.py", "--per-title", "3"], timeout=4 * 3600, env=env)
    for line in (r.stdout or "").splitlines()[-40:]:
        log("clip_fetch> " + line[-200:])
    tick()
    return max(0, len(release_assets("sources")) - before)


def record_status(done: list[dict]) -> None:
    """Mark requests done in config/clip_fetch_status.json (box.last_upload_at feeds clips.yml staleness)."""
    git("fetch", "-q", "origin", "main")
    git("reset", "-q", "--hard", "origin/main")
    p = REPO / "config" / "clip_fetch_status.json"
    s = json.loads(p.read_text()) if p.exists() else {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    box = s.setdefault("box", {})
    box["last_run_at"] = now
    box["last_upload_at"] = now
    s["done_ids"] = sorted(set(s.get("done_ids") or []) | {d["id"] for d in done if d.get("id")})[-3000:]
    s["clips"] = (s.get("clips") or [])[-400:] + [
        {k: d.get(k) for k in ("asset", "id", "title", "channel", "kind", "res", "by", "at")} for d in done]
    p.write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
    commit_push(["config/clip_fetch_status.json"], f"clips(box): restored {len(done)} official scene clip(s)")


def step_refill(st: dict) -> None:
    """Footage refill (config/refill_requests.json from refill.yml): fresh scenes of winning titles.
    clip_fetch stops early when YouTube/X throttle this box, so a 429 never turns into a hammering loop."""
    p = REPO / "config" / "refill_requests.json"
    try:
        reqs = json.loads(p.read_text()).get("requests") or []
    except Exception:
        reqs = []
    if not reqs:
        return
    env = dict(os.environ, CLIP_WORK=str(WORK / "refill"), CLIP_REQUESTS_FILE=str(p))
    r = run([sys.executable, "scripts/clip_fetch.py", "--per-title", "4"], timeout=3 * 3600, env=env)
    for line in (r.stdout or "").splitlines()[-30:]:
        log("refill> " + line[-200:])


def step_requests(st: dict) -> None:
    """Gemini's daily clip requests (config/clip_requests.json from clips.yml) -> YouTube/X on the box."""
    env = dict(os.environ, CLIP_WORK=str(WORK / "requests"))
    r = run([sys.executable, "scripts/clip_fetch.py", "--per-title", "4"], timeout=4 * 3600, env=env)
    for line in (r.stdout or "").splitlines()[-30:]:
        log("clip_fetch> " + line[-200:])


# ---------------------------------------------------------------- music
def _cached(fname: str) -> Path | None:
    for d in MUSIC_CACHE:
        p = d / fname
        if p.exists() and p.stat().st_size > 80_000:
            return p
    return None


def restore_track(t: dict) -> tuple[Path | None, dict]:
    """Rebuild one library mp3 with the SAME filename. Returns (file, metadata updates)."""
    import ingest_music as im
    fname = t["file"]
    WORK.mkdir(parents=True, exist_ok=True)
    c = _cached(fname)
    if c:
        return c, {}
    out = WORK / fname
    if t.get("source") == "archive.org" and t.get("source_id") and t.get("source_file"):
        raw = WORK / f"_raw_{fname}"
        url = im.IA_DL.format(id=urllib.parse.quote(t["source_id"]), name=urllib.parse.quote(t["source_file"]))
        if not im.download(url, raw, timeout=300):
            return None, {}
        cfg = json.loads((REPO / "config" / "music_sources.json").read_text())
        pre = im.analyze(raw) or {}
        ok = im.loudnorm_trim(raw, out, drop_t=pre.get("drop_t"), max_keep=float(cfg.get("max_keep_s") or 80),
                              pre_drop=float(cfg.get("pre_drop_s") or 12))
        raw.unlink(missing_ok=True)
        if not ok or not out.exists():
            return None, {}
        meta = im.analyze(out) or {}
        upd = {k: meta[k] for k in ("bpm", "drop_t", "duration_s", "energy") if k in meta}
        return out, upd
    if t.get("source_url") and URL_OK.match(t["source_url"]):
        base = WORK / Path(fname).stem
        r = run(["yt-dlp", "-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "160K",
                 "--no-playlist", "--no-warnings", *js_runtime_args(), *browser_args(), "-o", str(base) + ".%(ext)s", t["source_url"]],
                timeout=900)
        mp3 = base.with_suffix(".mp3")
        if r.returncode == 0 and mp3.exists():
            return mp3, {}
    return None, {}


def swap_track(lost: dict, lib: dict, taken: set[str]) -> dict | None:
    """Same-mood replacement from legit CC sources (Internet Archive CC0/BY/BY-SA), credited."""
    import ingest_music as im
    import music_lib
    cat = music_lib.track_cat(lost)
    q = {
        "love": '(romantic OR "love song" OR romance) AND mediatype:(audio) AND licenseurl:*creativecommons*',
        "cinematic": '(cinematic OR epic OR orchestral) AND mediatype:(audio) AND licenseurl:*creativecommons*',
        "hiphop": '(hiphop OR "hip hop" OR trap) AND mediatype:(audio) AND licenseurl:*creativecommons*',
    }.get(cat, '(phonk OR "drift phonk" OR "brazilian funk") AND mediatype:(audio) AND licenseurl:*creativecommons*')
    cfg = json.loads((REPO / "config" / "music_sources.json").read_text())
    have_ids = {x.get("source_id") for x in lib["tracks"]} | set(lib.get("_tried_ids") or [])
    for item in im.ia_search(q, rows=60):
        ident = im.meta_text(item.get("identifier"), "")
        if not ident or ident in have_ids:
            continue
        have_ids.add(ident)
        lib.setdefault("_tried_ids", []).append(ident)
        for fi in im.ia_audio_files(ident)[:1]:
            t = im.process_candidate(item, fi, lib, cfg, taken)
            if t and not music_lib.is_denied_track(t, REPO):
                if music_lib.track_cat(t) != cat and cat in music_lib.MUSIC_CATS:
                    t["cat"] = cat
                t["replaces"] = lost["file"]
                return t
    return None


def step_music(st: dict, tick=lambda: None, limit: int = 0) -> dict:
    """Restore every music/library.json track missing on the `music` release (same filename); swap the
    unrecoverable ones for same-mood credited songs; drop blocked songs from the library."""
    import music_lib
    lp = REPO / "music" / "library.json"
    lib = json.loads(lp.read_text())
    have = release_assets("music")
    lost_p = REPO / "music" / "lost_tracks.json"
    lost_doc = json.loads(lost_p.read_text()) if lost_p.exists() else {"note": "", "tracks": []}
    lost_doc["note"] = ("Library tracks whose mp3 was lost with the suspended chatgpt820970 account and could not be "
                        "re-downloaded, or that the Content ID block-list removed. Kept for history; not used.")
    restored, swapped, blocked, failed = [], [], [], []
    changed = False
    keep = []
    for t in lib["tracks"]:
        if music_lib.is_blocked_track(t, REPO):
            blocked.append(t["file"])
            lost_doc["tracks"].append(dict(t, lost_reason="content-id block-list"))
            changed = True
            continue
        keep.append(t)
    lib["tracks"] = keep
    missing = [t for t in lib["tracks"] if t["file"] not in have]
    log(f"music: {len(missing)} of {len(lib['tracks'])} library tracks missing on the release")
    taken = {t["file"] for t in lib["tracks"]} | set(have)
    for t in missing:
        if limit and len(restored) + len(swapped) >= limit:
            break
        tick()
        key = f"music:{t['file']}"
        if not tries_ok(st, key):
            continue
        try:
            f, upd = restore_track(t)
        except Exception as e:
            f, upd = None, {}
            log(f"music: {t['file']} restore error {type(e).__name__}")
        if f and upload("music", f if f.name == t["file"] else _renamed(f, t["file"])):
            if upd:
                t.update(upd)
                changed = True
            restored.append(t["file"])
            if f.parent == WORK:
                f.unlink(missing_ok=True)
            continue
        count_try(st, key, "restore failed")
        if int(((st.get("tries") or {}).get(key) or {}).get("n") or 0) < 2:
            failed.append(t["file"])
            continue
        new = swap_track(t, lib, taken)
        if new and upload("music", REPO / "music" / "lib" / new["file"]):
            lib["tracks"] = [x for x in lib["tracks"] if x["file"] != t["file"]] + [new]
            lost_doc["tracks"].append(dict(t, lost_reason="not re-downloadable", replaced_by=new["file"]))
            swapped.append((t["file"], new["file"]))
            changed = True
        else:
            failed.append(t["file"])
    lib.pop("_tried_ids", None)
    if changed:
        lp.write_text(json.dumps(lib, indent=1, ensure_ascii=False) + "\n")
        lost_p.write_text(json.dumps(lost_doc, indent=1, ensure_ascii=False) + "\n")
        commit_push(["music/library.json", "music/lost_tracks.json"],
                    f"music(box): restored {len(restored)}, swapped {len(swapped)}, blocked {len(blocked)}")
    st["music_summary"] = {"at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "missing": len(missing),
                           "restored_now": len(restored), "swapped_now": len(swapped), "blocked_now": blocked,
                           "failed_now": len(failed)}
    st.setdefault("music_swaps", []).extend(swapped)
    save_state(st)
    return st["music_summary"]


def _renamed(f: Path, name: str) -> Path:
    dst = WORK / name
    if f.resolve() != dst.resolve():
        shutil.copyfile(f, dst)
    return dst


# ---------------------------------------------------------------- workflow backup timer (ex skywalker-timer)
def secrets_ready() -> set[str]:
    r = gh("secret", "list", "-R", GH_REPO, "--json", "name", "-q", ".[].name", timeout=60)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def step_dispatch(st: dict) -> None:
    """Dispatch a workflow only when GitHub's own cron is late. Off until the secrets it needs exist."""
    if os.environ.get("SKY_BOX_DISPATCH", "0") != "1":  # off by default: health/render/publish can post
        return
    names = secrets_ready()
    if "GEMINI_API_KEY" not in names:
        return
    now = datetime.now(IST)
    for wf, (max_age, earliest) in JOBS.items():
        if wf == "publish.yml" and ("BUFFER_API_KEY" not in names or os.environ.get("SKY_BOX_PUBLISH", "0") != "1"):
            continue
        if earliest and now.strftime("%H:%M") < earliest:
            continue
        r = gh("run", "list", "-R", GH_REPO, "-w", wf, "-L", "1", "--json", "createdAt", timeout=60)
        if r.returncode != 0:
            continue
        rows = json.loads(r.stdout or "[]")
        age = 1e9
        if rows:
            t = datetime.fromisoformat(rows[0]["createdAt"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - t).total_seconds() / 60
        if age > max_age:
            d = gh("workflow", "run", wf, "-R", GH_REPO, timeout=60)
            log(f"dispatch {wf}: last run {age:.0f} min ago > {max_age} -> {'ok' if d.returncode == 0 else 'FAILED'}")


def make_tick(st: dict):
    def tick():
        if time.time() - _LAST_DISPATCH[0] >= 300:
            _LAST_DISPATCH[0] = time.time()
            try:
                step_dispatch(st)
            except Exception as e:
                log(f"dispatch error {type(e).__name__}")
            heartbeat(st, upload_it=False)
    return tick


# ---------------------------------------------------------------- heartbeat / status
def heartbeat(st: dict, upload_it: bool = True) -> None:
    hb = {"last_run": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"), "pid": os.getpid(),
          "host": os.uname().nodename, "clips": st.get("clips_summary"), "music": st.get("music_summary"),
          "browser_auth": bool(browser_args()), "repo": GH_REPO}
    p = HOME / "box_heartbeat.json"
    p.write_text(json.dumps(hb, indent=1))
    if upload_it and due(st, "hb_upload", 1800):
        if gh("release", "upload", "sources", str(p), "-R", GH_REPO, "--clobber", timeout=120).returncode == 0:
            mark(st, "hb_upload")


def status() -> int:
    st = load_state()
    print(json.dumps({"heartbeat": json.loads((HOME / "box_heartbeat.json").read_text())
                      if (HOME / "box_heartbeat.json").exists() else None,
                      "clips": st.get("clips_summary"), "music": st.get("music_summary"),
                      "last": {k: datetime.fromtimestamp(v, IST).strftime("%m-%d %H:%M")
                               for k, v in (st.get("last") or {}).items()}}, indent=1))
    try:
        have = release_assets("sources")
        miss = [n["asset"] for n in needed_clips() if n["asset"] not in have]
        lib = json.loads((REPO / "music" / "library.json").read_text())
        mh = release_assets("music")
        print(f"scene clips still missing: {len(miss)}; music tracks missing: "
              f"{sum(1 for t in lib['tracks'] if t['file'] not in mh)} of {len(lib['tracks'])}")
    except Exception as e:
        print("release check failed:", e)
    return 0


def cycle(only: str = "") -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    lock = open(HOME / "cycle.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another cycle is running; skip")
        return 0
    st = load_state()
    tick = make_tick(st)
    steps = [
        ("dispatch", 0, lambda: step_dispatch(st)),
        ("links", 0, lambda: step_links(st)),
        ("clips", 1200, lambda: log(f"clips: {step_clips(st, tick)}")),
        ("music", 3600, lambda: log(f"music: {step_music(st, tick)}")),
        ("refill", 2 * 3600, lambda: step_refill(st)),
        ("requests", 6 * 3600, lambda: step_requests(st)),
    ]
    for name, every, fn in steps:
        if only and name != only:
            continue
        if not only and every and not due(st, name, every):
            continue
        try:
            fn()
            if every:
                mark(st, name)
        except Exception:
            log(f"{name} crashed: {traceback.format_exc()[-600:]}")
    heartbeat(st)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["cycle", "sync", "status", "clips", "links", "music", "refill", "requests", "dispatch"])
    a = ap.parse_args()
    if a.cmd == "sync":
        log("sync: " + sync_code())
        return 0
    if a.cmd == "status":
        return status()
    return cycle("" if a.cmd == "cycle" else a.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
