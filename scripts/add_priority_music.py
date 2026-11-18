#!/usr/bin/env python3
"""Add the owner's priority songs to the edit library (run on the box, NOT on runners).

GitHub runners get bot-blocked by YouTube, so this runs where yt-dlp works:
  1. reads config/priority_music.json (single links + playlists, each with a cat)
  2. downloads audio with yt-dlp (no cookies), converts to mp3
  3. analyses bpm / drop / high-energy points with ffmpeg + numpy (beat_sync, no librosa needed)
  4. uploads the mp3 to the 'music' release (same place as the other tracks, not git)
  5. appends music/library.json rows with priority=high and cat=action|love|cinematic|hiphop

Idempotent: a source URL already in the library is skipped. Denylisted songs are never added.
Usage: python3 scripts/add_priority_music.py [--dry-run] [--no-upload] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import beat_sync  # noqa: E402
import music_lib  # noqa: E402

CFG = REPO / "config" / "priority_music.json"
LIB = REPO / "music" / "library.json"
WORK = Path("/tmp/sk_priority_music")

CAT_META = {
    "action": {"genre": "phonk", "mood": "hype", "pillars": ["anime", "movie", "celeb"], "tags": ["phonk", "edit", "priority"]},
    "love": {"genre": "romantic", "mood": "romantic", "pillars": ["romance"], "tags": ["love", "romantic", "priority"]},
    "cinematic": {"genre": "cinematic", "mood": "epic", "pillars": ["movie", "anime"], "tags": ["cinematic", "epic", "priority"]},
    "hiphop": {"genre": "hip-hop", "mood": "swag", "pillars": ["celeb", "movie", "anime"], "tags": ["hip-hop", "edit", "priority"]},
}
_AUTO = [
    ("love", ("love", "romantic", "romance", "piano", "heart", "kiss", "lofi", "sad", "emotional", "instrumental")),
    ("hiphop", ("hip hop", "hip-hop", "rap", "trap", "drill", "boom bap")),
    ("cinematic", ("cinematic", "epic", "orchestral", "trailer", "score", "interstellar", "zimmer")),
    ("action", ("phonk", "funk", "montagem", "drift", "slowed", "sped up", "bass", "edit")),
]


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())[:n] or "track"


def guess_cat(title: str, default: str = "action") -> str:
    t = (title or "").lower()
    for cat, words in _AUTO:
        if any(w in t for w in words):
            return cat
    return default


def high_points(y: np.ndarray, sr: int, beats: list[float], n: int = 4, gap: float = 8.0,
                tail: float = 10.0) -> list[float]:
    """Biggest sustained energy jumps (drops), >= gap apart, snapped to beats, ranked best first."""
    hop = max(1, int(0.25 * sr))
    m = len(y) // hop
    if m < 24:
        return []
    rms = np.sqrt(np.mean(y[: m * hop].reshape(m, hop) ** 2, axis=1) + 1e-12)
    rms = rms / (rms.max() + 1e-12)
    k = 12  # 3 s each side
    dur = len(y) / sr
    cand = []
    for i in range(k, m - k):
        t = i * 0.25
        if t > dur - tail:
            break
        jump = float(rms[i:i + k].mean() - rms[i - k:i].mean()) + 0.35 * float(rms[i:i + k].mean())
        cand.append((jump, t))
    cand.sort(reverse=True)
    out: list[float] = []
    b = np.asarray(beats, dtype=float)
    for _, t in cand:
        if b.size:
            t = float(b[int(np.argmin(np.abs(b - t)))])
        if all(abs(t - o) >= gap for o in out):
            out.append(round(t, 2))
        if len(out) >= n:
            break
    return out


def analyse(path: Path) -> dict | None:
    loaded = beat_sync._ffmpeg_mono(path)
    if not loaded:
        return None
    y, sr = loaded
    dur = float(len(y) / sr)
    if dur < 25:
        return None
    info = beat_sync.detect_beats(path)
    highs = high_points(y, sr, info.get("beats") or [])
    hop = 512
    m = len(y) // hop
    rms = np.sqrt(np.mean(y[: m * hop].reshape(m, hop) ** 2, axis=1) + 1e-12)
    energy = float(np.clip(np.mean(rms) / (np.percentile(rms, 90) + 1e-9), 0, 1))
    return {"bpm": round(float(info.get("tempo") or 100.0), 1), "drop_t": highs[0] if highs else 12.0,
            "highs": highs, "duration_s": round(dur, 1), "energy": round(energy, 3)}


def expand(cfg: dict) -> list[dict]:
    rows = [dict(r) for r in cfg.get("tracks") or [] if r.get("url")]
    for pl in cfg.get("playlists") or []:
        url = pl.get("url")
        if not url:
            continue
        r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", url], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  playlist failed: {url}: {(r.stderr or '')[-200:]}")
            continue
        for e in (json.loads(r.stdout).get("entries") or []):
            vid = e.get("id")
            if not vid:
                continue
            rows.append({"url": f"https://www.youtube.com/watch?v={vid}", "title": e.get("title") or "",
                         "artist": e.get("uploader") or e.get("channel") or "",
                         "cat": pl.get("cat") or "auto", "from": url})
    return rows


def download(url: str, out_base: Path) -> tuple[Path | None, dict]:
    out_base.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["yt-dlp", "-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "160K",
                        "--no-playlist", "--print-json", "-o", str(out_base) + ".%(ext)s", url],
                       capture_output=True, text=True)
    meta = {}
    for line in (r.stdout or "").splitlines():
        try:
            meta = json.loads(line)
        except Exception:
            pass
    mp3 = out_base.with_suffix(".mp3")
    if r.returncode != 0 or not mp3.exists():
        print(f"  download failed: {url}: {(r.stderr or '')[-200:]}")
        return None, meta
    return mp3, meta


def build_row(fname: str, src: dict, meta: dict, a: dict) -> dict:
    cat = src.get("cat") or "auto"
    title = src.get("title") or meta.get("track") or meta.get("title") or fname
    artist = src.get("artist") or meta.get("artist") or meta.get("uploader") or ""
    if cat not in music_lib.MUSIC_CATS:
        cat = guess_cat(f"{title} {meta.get('title') or ''}")
    cm = CAT_META[cat]
    return {
        "file": fname, "title": title, "artist": artist, "genre": cm["genre"], "mood": cm["mood"],
        "pillars": list(cm["pillars"]), "cat": cat, "priority": "high",
        "bpm": a["bpm"], "drop_t": a["drop_t"], "highs": a["highs"], "duration_s": a["duration_s"],
        "energy": a["energy"], "tags": sorted(set(cm["tags"])),
        "credit": f"Song: {artist} - {title}" if artist else f"Song: {title}",
        "source": "youtube", "source_url": src["url"], "safety": "claim_risk",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    cfg = json.loads(CFG.read_text())
    lib = json.loads(LIB.read_text())
    have_urls = {t.get("source_url") for t in lib["tracks"] if t.get("source_url")}
    have_files = {t["file"] for t in lib["tracks"]}
    todo = [r for r in expand(cfg) if r["url"] not in have_urls]
    if args.limit:
        todo = todo[: args.limit]
    print(f"priority music: {len(todo)} new link(s)")
    added = []
    for src in todo:
        if music_lib.is_denied_track({"title": src.get("title"), "artist": src.get("artist")}):
            print(f"  banned, skip: {src.get('title')}")
            continue
        if args.dry_run:
            print(f"  would add [{src.get('cat')}] {src.get('title') or src['url']}")
            continue
        base = WORK / f"pr_{slug(src.get('title') or src['url'].split('=')[-1])}"
        mp3, meta = download(src["url"], base)
        if not mp3:
            continue
        a = analyse(mp3)
        if not a:
            print(f"  analysis failed/too short: {mp3.name}")
            continue
        fname = mp3.name
        n = 2
        while fname in have_files:
            fname = f"{mp3.stem}{n}.mp3"
            n += 1
        row = build_row(fname, src, meta, a)
        if music_lib.is_denied_track(row):
            print(f"  banned, skip: {row['title']}")
            continue
        if fname != mp3.name:
            mp3 = mp3.rename(mp3.with_name(fname))
        if not args.no_upload:
            r = subprocess.run(["gh", "release", "upload", lib.get("release") or "music", str(mp3), "--clobber",
                                "-R", "chatgpt820970/skywalker-shorts"], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  upload failed: {fname}: {(r.stderr or '')[-200:]}")
                continue
        lib["tracks"].append(row)
        have_files.add(fname)
        have_urls.add(src["url"])
        added.append(row)
        LIB.write_text(json.dumps(lib, indent=2, ensure_ascii=False) + "\n")
        print(f"  + [{row['cat']}] {fname} bpm={row['bpm']} drop={row['drop_t']} highs={row['highs']}")
    from collections import Counter
    print("added per cat:", dict(Counter(r["cat"] for r in added)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
