#!/usr/bin/env python3
"""Fetch official scene clips listed in config/clip_requests.json -> 'sources' release + topic catalog.

Box:    python3 scripts/clip_fetch.py               (YouTube official channels + DM/Bilibili)
GitHub: python3 scripts/clip_fetch.py --no-youtube  (DailyMotion / Bilibili only; YouTube blocks GitHub)

Rules: official channels only, no fan reuploads/watermark farms, official SCENE clips only
(never trailers/teasers, even as fallback), 3+ clips per title, >=1080p (up to 4K), bitrate gate,
no re-encode, idempotent (done_ids + release asset check), retry with backoff, local files
deleted after upload. Never prints secrets.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import overlay_guard  # noqa: E402
import sourcing  # noqa: E402

GH_REPO = "Bhupi820970/skywalker-shorts"
IST = timezone(timedelta(hours=5, minutes=30))
REQ = Path(os.environ.get("CLIP_REQUESTS_FILE") or REPO / "config" / "clip_requests.json")
STATUS = REPO / "config" / "clip_fetch_status.json"
CATALOG = REPO / "config" / "topic_catalog.json"
DENY = REPO / "config" / "clip_denylist.json"
WORK = Path(os.environ.get("CLIP_WORK", "/tmp/sky_clips"))
LOG = os.environ.get("CLIP_LOG")  # box: /workspace/skywalker-cron.log
FMT = "bv*[height>=1080][height<=2160]+ba/b[height>=1080][height<=2160]"
SORT = "res:2160,vcodec:vp9,acodec:m4a"  # highest res <=4K; avoid AV1 when VP9/H264 exist
TRAILER = re.compile(r"\b(trailer|teaser|promo|tv spot)\b", re.I)
JUNK = re.compile(r"(#shorts|\bedit\b|amv|status|whatsapp|4k\s*edit|compilation|scene pack|clip pack|"
                  r"hindi dubbed|1080p60 fan|slowed|reverb|lyrics|reaction|explained|recap|review)", re.I)


def log(msg: str) -> None:
    line = f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST clip_fetch: {msg}"
    if not LOG:
        print(line, flush=True)
    else:
        with open(LOG, "a") as f:
            f.write(line + "\n")


def run(cmd, timeout=600, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def retry(fn, tries=3, base=8):
    for i in range(tries):
        try:
            r = fn()
            if r:
                return r
        except Exception as e:  # network / timeout
            log(f"retry {i + 1}/{tries}: {type(e).__name__}")
        time.sleep(base * (3 ** i))
    return None


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def denied_ids() -> set[str]:
    """Permanent fetch denylist (yt:<id> plus the bare id)."""
    ids: set[str] = set()
    try:
        for x in json.loads(DENY.read_text()).get("ids") or []:
            s = str(x).strip().lower()
            if not s:
                continue
            ids.add(s)
            bare = s.split(":", 1)[-1]
            if bare:
                ids.add(bare)
                ids.add(f"yt:{bare}")
    except Exception:
        pass
    return ids


def is_denied_clip(cid: str, url: str = "") -> bool:
    deny = denied_ids()
    tokens = [cid or "", url or ""]
    for token in tokens:
        s = str(token).strip().lower()
        if not s:
            continue
        if s in deny:
            return True
        bare = s.split(":", 1)[-1]
        if bare in deny or f"yt:{bare}" in deny:
            return True
        m = re.search(r"(?:v=|/youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,})", s)
        if m and (m.group(1).lower() in deny or f"yt:{m.group(1).lower()}" in deny):
            return True
    return False


def official_set(req: dict) -> set[str]:
    chans = set(norm(c) for c in req.get("official_channels") or [])
    try:
        for lst in (json.loads((REPO / "config" / "source_catalog.json").read_text()).get("official_channels") or {}).values():
            chans |= {norm(c) for c in lst}
    except Exception:
        pass
    return {c for c in chans if len(c) >= 3 and not sourcing.is_logo_burn_channel(c)}


def is_official(entry: dict, chans: set[str]) -> bool:
    ch = norm(entry.get("channel") or entry.get("uploader") or "")
    return bool(ch) and any(ch == c or (len(c) >= 5 and c in ch) for c in chans)


def search(req: dict, youtube: bool) -> list[dict]:
    words = req.get("words") or req["slug"]
    scene_q = [q for q in (req.get("search_queries") or []) if q and not TRAILER.search(q)][:6]
    if not scene_q:
        scene_q = [f"{words} {t} scene official clip" for t in (req.get("scene_types") or ["fight", "emotional", "aura"])]
    prefixes = (["ytsearch10"] if youtube else []) + ["dmsearch5", "bilibilisearch3"]
    chans = official_set(req)
    found, seen = [], set()
    # 1) link mode: explicit official links in the request, X posts before YouTube.
    links = [u for u in (req.get("links") or []) if isinstance(u, str) and u.startswith("https://")]
    links.sort(key=lambda u: 0 if re.search(r"https://(www\.)?(x|twitter)\.com/", u) else 1)
    for u in links[:10]:
        if TRAILER.search(u) or sourcing.is_trailer_source(u) or u in seen:
            continue
        seen.add(u)
        m = re.search(r"(?:v=|youtu\.be/|/shorts/|/status/)([\w-]{6,})", u)
        pfx = "x" if re.search(r"(x|twitter)\.com/", u) else "yt"
        found.append({"id": f"{pfx}:{m.group(1) if m else u}", "url": u, "title": req.get("words") or req["slug"],
                      "kind": "scene", "channel": "link", "link": True})
    # 2) official X accounts for the title (before YouTube).
    try:
        for e in sourcing.search_official_x_entries({"words": words, "slug": req["slug"],
                                                     "official_channels": req.get("official_channels") or []},
                                                    limit=4):
            t = e.get("title") or ""
            if not e.get("url") or e["url"] in seen or TRAILER.search(t) or JUNK.search(t):
                continue
            seen.add(e["url"])
            found.append({"id": e.get("id") or f"x:{e['url']}", "url": e["url"], "title": t[:200],
                          "kind": "scene", "channel": e.get("channel") or e.get("handle") or "x", "x": True})
    except Exception as ex:
        log(f"{req.get('slug')}: x search skipped ({type(ex).__name__})")
    n_first = len(found)
    for q in scene_q:
        for pfx in prefixes:
            r = run(["yt-dlp", "--flat-playlist", "-J", "--no-warnings", f"{pfx}:{q}"], timeout=90)
            if r.returncode != 0:
                continue
            try:
                entries = json.loads(r.stdout).get("entries") or []
            except Exception:
                continue
            for e in entries:
                vid, title = e.get("id"), e.get("title") or ""
                dur = e.get("duration") or 0
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                if sourcing._BAD_TITLE.search(title) or sourcing._WATERMARK_HINT.search(title) or JUNK.search(title):
                    continue
                if TRAILER.search(title) or sourcing.is_trailer_source(title):
                    continue  # never download trailers/teasers
                if dur and not (20 <= dur <= 480):
                    continue
                if not is_official(e, chans):
                    continue
                ch = e.get("channel") or e.get("uploader") or ""
                if sourcing.is_logo_burn_channel(ch):
                    continue  # Crunchyroll / Muse / Ani-One burn a logo or subs
                url = e.get("webpage_url") or e.get("url") or ""
                if pfx.startswith("yt") and not url.startswith("http"):
                    url = f"https://www.youtube.com/watch?v={vid}"
                found.append({"id": f"{pfx[:2]}:{vid}", "url": url, "title": title, "kind": "scene",
                              "channel": ch})
    head, rest = found[:n_first], found[n_first:]
    rest.sort(key=lambda c: sourcing.channel_preference(c.get("channel") or ""))
    return head + rest


def probe(p: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,codec_name,bit_rate:format=duration,bit_rate,size", "-of", "json", str(p)], timeout=60)
    d = json.loads(r.stdout or "{}")
    s = (d.get("streams") or [{}])[0]
    f = d.get("format") or {}
    dur = float(f.get("duration") or 0)
    kbps = int(s.get("bit_rate") or 0) // 1000 or (int(f.get("size") or 0) * 8 // max(dur, 1) // 1000)
    return {"w": int(s.get("width") or 0), "h": int(s.get("height") or 0), "codec": s.get("codec_name"),
            "kbps": int(kbps), "dur": dur}


def quality_ok(m: dict) -> str | None:
    short = min(m["w"], m["h"])
    if short < 1080:
        return f"{m['w']}x{m['h']} below 1080p"
    need = int(1300 * ((m["w"] * m["h"]) / 2_073_600) ** 0.75)
    if m["kbps"] and m["kbps"] < need:
        return f"low bitrate {m['kbps']}kbps < {need}"
    if m["dur"] < 15:
        return f"too short {m['dur']:.0f}s"
    return None


def browser_cookie_args() -> list[str]:
    """Box only, opt-in: SKY_YTDLP_BROWSER=chrome:/path/to/profile lets yt-dlp read the box browser's
    YouTube / X login for this download. Cookies stay in the browser profile; they are never copied into
    the repo, a release, a log or a manifest. Never set this on GitHub runners."""
    b = (os.environ.get("SKY_YTDLP_BROWSER") or "").strip()
    if not b or b.startswith("-") or any(ch in b for ch in ";|&`$\n"):
        return []
    return ["--cookies-from-browser", b]


def download(url: str, dest: Path) -> bool:
    for f in WORK.glob(dest.stem + "*"):
        f.unlink(missing_ok=True)
    cmd = ["yt-dlp", "--no-playlist", "--remote-components", "ejs:github", "-f", FMT, "-S", SORT,
           "--merge-output-format", "mp4", "-o", str(dest), "--no-warnings", "--no-part",
           *browser_cookie_args(), url]
    r = run(cmd, timeout=900)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 1_000_000


def release_assets() -> set[str]:
    r = run(["gh", "api", "--paginate", f"repos/{GH_REPO}/releases/tags/sources", "-q", ".assets[].name"], timeout=120)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def upload(p: Path) -> bool:
    r = run(["gh", "release", "upload", "sources", str(p), "-R", GH_REPO], timeout=1800)
    if r.returncode != 0 and "already exists" in (r.stderr or ""):
        return True
    return r.returncode == 0


def apply_records(records: list[dict], who: str, requests_done: list[str]) -> None:
    """Merge uploaded clips into topic_catalog + status (re-read from disk so it is rebase-safe)."""
    cat = json.loads(CATALOG.read_text())
    items = cat.setdefault("items", [])
    by = {t["slug"]: t for t in items}
    for rec in records:
        req = rec["req"]
        t = by.get(req["slug"])
        if t is None:
            tags = [norm(x) for x in (req.get("tags") or [])][:8] or [req["slug"], "edit", "shorts"]
            hook = req.get("hook") or f"Watch {req.get('words', '').title()}"
            t = {"slug": req["slug"], "pillar": req.get("pillar", "hollywood"), "words": req.get("words", req["slug"].upper()),
                 "hook": hook, "title": req.get("title") or f"{hook} | {req.get('words', '').title()} | #edit",
                 "description": f"{hook}. From official {req.get('words', '').title()} clips. Edit by Skywalker.\n"
                                "Source: official studio/distributor clip. All rights to the film/series belong to their owners; short commentary edit.\n"
                                + " ".join("#" + x for x in ["edit", "shorts"] + tags[:3]),
                 "tags": tags, "market": req.get("market", "US/UK"), "source_url": rec["url"],
                 "scene_assets": [], "content_kind": "best scene", "added_by": f"clip_fetch:{who}"}
            items.append(t)
            by[t["slug"]] = t
        if rec["kind"] == "trailer" or sourcing.is_trailer_source(rec.get("asset") or rec.get("title") or ""):
            continue  # never index trailers
        if rec["asset"] not in (t.get("scene_assets") or []):
            t.setdefault("scene_assets", []).append(rec["asset"])
        t["content_kind"] = "best scene"
    CATALOG.write_text(json.dumps(cat, indent=2, ensure_ascii=False) + "\n")
    st = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = st.setdefault(who, {})
    s["last_run_at"] = now
    if records:
        s["last_upload_at"] = now
    done = set(st.get("done_ids") or []) | {r["id"] for r in records}
    st["done_ids"] = sorted(done)[-3000:]
    st["clips"] = (st.get("clips") or [])[-400:] + [{k: r[k] for k in ("asset", "id", "title", "channel", "kind", "res", "kbps")} | {"by": who, "at": now} for r in records]
    STATUS.write_text(json.dumps(st, indent=1, ensure_ascii=False) + "\n")


def commit(records, who, done, push: bool) -> None:
    if not push:
        apply_records(records, who, done)
        return
    for i in range(5):
        run(["git", "fetch", "-q", "origin", "main"], timeout=120)
        run(["git", "reset", "-q", "--hard", "origin/main"], timeout=60)
        apply_records(records, who, done)
        run(["git", "add", str(CATALOG), str(STATUS)])
        msg = f"clips({who}): +{len(records)} official clip(s) " + ",".join(sorted({r['req']['slug'] for r in records}))[:120]
        c = run(["git", "-c", "user.name=skywalker-clips", "-c", "user.email=actions@users.noreply.github.com",
                 "commit", "-q", "-m", msg])
        if c.returncode != 0:
            return
        if run(["git", "push", "-q", "origin", "HEAD:main"], timeout=120).returncode == 0:
            log(f"committed: {msg}")
            return
        time.sleep(5 * (i + 1))
    log("WARN: index commit failed after retries (clips are on release; next run re-adds)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-youtube", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max requests (0 = all)")
    ap.add_argument("--per-title", type=int, default=0)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--slugs", default="", help="comma-separated slugs to fetch (default: all requests)")
    a = ap.parse_args()
    who = "github" if a.no_youtube else "box"
    if not REQ.exists():
        log("no config/clip_requests.json; nothing to do")
        return 0
    doc = json.loads(REQ.read_text())
    reqs = doc.get("requests") or []
    if a.slugs:
        want = {norm(s) for s in a.slugs.split(",") if s.strip()}
        reqs = [r for r in reqs if norm(r.get("slug") or "") in want]
    if a.limit:
        reqs = reqs[: a.limit]
    per = a.per_title or int(doc.get("max_clips_per_title") or 5)
    per = max(per, 3)  # always grab 3-5 official scene clips per title when they exist
    st = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    done_ids = set(st.get("done_ids") or [])
    have = retry(release_assets, 3, 5) or set()
    WORK.mkdir(parents=True, exist_ok=True)
    total, fails = 0, 0
    for req in reqs:
        slug = req.get("slug")
        if not slug:
            continue
        if sum(1 for x in have if x.startswith(f"{slug}_scene")) >= per:
            log(f"{slug}: already has {per}+ scene clips on the release; request done")
            continue
        try:
            cands = retry(lambda: search(req, not a.no_youtube) or None, 2, 10) or []
        except Exception as e:
            log(f"{slug}: search error {type(e).__name__}; next title")
            fails += 1
            continue
        got, records, scenes = 0, [], 0
        for c in cands:
            if got >= per:
                break
            if c["id"] in done_ids or is_denied_clip(c.get("id") or "", c.get("url") or ""):
                continue
            if c["kind"] == "trailer" or TRAILER.search(c.get("title") or "") or sourcing.is_trailer_source(c.get("url") or ""):
                continue  # never download trailers
            n = 1
            while f"{slug}_scene{n}_h264.mp4" in have:
                n += 1
            asset = f"{slug}_scene{n}_h264.mp4"
            dest = WORK / asset
            try:
                ok = retry(lambda: download(c["url"], dest), 2, 15)
                if not ok:
                    log(f"{slug}: download failed {c['id']}; next source")
                    continue
                m = probe(dest)
                bad = quality_ok(m)
                if bad:
                    log(f"{slug}: reject {c['id']} ({bad})")
                    done_ids.add(c["id"])
                    continue
                why = overlay_guard.clip_reason(dest)
                if why:
                    log(f"{slug}: reject {c['id']} ({why}); skip, do not crop")
                    done_ids.add(c["id"])
                    continue
                if not retry(lambda: upload(dest), 3, 10):
                    log(f"{slug}: upload failed {asset}; next source")
                    continue
                have.add(asset)
                done_ids.add(c["id"])
                records.append({"req": req, "id": c["id"], "url": c["url"], "title": c["title"][:120],
                                "channel": c["channel"], "kind": c["kind"], "asset": asset,
                                "res": f"{m['w']}x{m['h']}", "kbps": m["kbps"]})
                got += 1
                scenes += c["kind"] == "scene"
                log(f"{slug}: uploaded {asset} {m['w']}x{m['h']} {m['codec']} {m['kbps']}kbps [{c['kind']}] {c['channel']}: {c['title'][:60]}")
            except Exception as e:
                log(f"{slug}: error {type(e).__name__} on {c['id']}; next source")
            finally:
                for f in WORK.glob(Path(asset).stem + "*"):
                    f.unlink(missing_ok=True)
        if records:
            commit(records, who, [], push=not a.no_push)
            total += len(records)
        else:
            log(f"{slug}: no official >=1080p clip found ({len(cands)} candidates)")
    commit([], who, [], push=not a.no_push)  # heartbeat (last_run_at)
    log(f"done: uploaded {total} clip(s) from {len(reqs)} request(s) ({who})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
