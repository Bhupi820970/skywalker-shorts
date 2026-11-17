"""Official video source chain for Skywalker Shorts.

YouTube often blocks GitHub runners ("Sign in to confirm you're not a bot").
download_source builds a long fallback chain: studio/press kits -> official
scene-clip sites -> official channels on other platforms -> Indian free clip
sites -> YouTube via yt-dlp last. Only free official SCENE clips (720p+).
No trailers or teasers, even as fallback. No DRM breaking, no paid OTT rips,
no third-party channel watermarks.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parent
CATALOG = REPO / "config" / "source_catalog.json"

# Prefer 720p+; avoid tiny/mobile encodes.
YTDLP_FORMAT = "bv*[height>=720][height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4][height>=720]/bv*[height>=720]+ba/b"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Studio / press / official brand hosts (checked first when resolving search URLs).
STUDIO_HOSTS = (
    "marvel.com", "disney.com", "disneyplus.com", "warnerbros.com", "wbd.com",
    "sonypictures.com", "universalpictures.com", "paramount.com", "paramountpictures.com",
    "lionsgate.com", "a24films.com", "tudum.netflix.com", "netflix.com",
    "primevideo.com", "amazon.com", "tv.apple.com", "trailers.apple.com",
    "crunchyroll.com", "toho.co.jp", "tseries.com", "yrf.com", "dharmaproductions.com",
    "hombalefilms.com", "mythrimovie.com", "sunpictures.in", "lycaproductions.in",
    "geethaarts.com",
)

TRAILER_CLIP_HOSTS = (
    "imdb.com", "rottentomatoes.com", "fandango.com", "movieclips.com",
    "trailers.apple.com", "tv.apple.com",
)

PLATFORM_HOSTS = (
    "dailymotion.com", "vimeo.com", "archive.org", "bilibili.com", "bilibili.tv",
    "facebook.com", "fb.watch", "instagram.com", "x.com", "twitter.com",
    "reddit.com", "v.redd.it", "tiktok.com",
)

INDIAN_HOSTS = (
    "hotstar.com", "jio.com", "zee5.com", "sonyliv.com", "mxplayer.in",
    "bollywoodhungama.com", "timesofindia.indiatimes.com", "indiatimes.com",
)

YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# Queue / catalog request kinds. Trailers/teasers are never sourced.
CONTENT_KINDS = (
    "official clip", "best scene", "action scene",
    "fight scene", "war scene", "romance scene", "love scene", "emotional scene",
)

_TRAILER_ASSET = re.compile(
    r"(?:^|[._-])(trailer|teaser|tvspot|tv_spot|tv-spot|promo)(?:[._-]|$)",
    re.I,
)

# Reject obvious fan edits / watermark farms in titles when searching.
_BAD_TITLE = re.compile(
    r"(fan[\s-]?edit|reaction|leak|camrip|hdcam|full\s*movie|download|"
    r"explained|review|recap|theory|tiktok\s*compilation)",
    re.I,
)
_WATERMARK_HINT = re.compile(
    r"(movieclips\s*extra|filmibeat|filmy|filmykeeday|t-series\s*stage\s*works)",
    re.I,
)

# Official YouTube uploads that burn a channel logo and/or subtitles into the
# picture (Crunchyroll, Muse, Ani-One). Never fetch or render these. Prefer
# clean studio / Netflix / Toho sources instead.
LOGO_BURN_CHANNELS = (
    "crunchyroll", "museasia", "museindia", "museanime", "musemalaysia",
    "anione", "anioneasia", "anioneindonesia", "anioneofficial",
    "funimation", "hidive",
)
PREFERRED_CHANNELS = (
    "netflix", "netflixanime", "netflixindia", "toho", "tohoanimation",
)


def _norm_chan(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_logo_burn_channel(name: str) -> bool:
    """True for channels that stamp a logo/subs onto official YouTube uploads."""
    n = _norm_chan(name)
    if not n:
        return False
    for c in LOGO_BURN_CHANNELS:
        if n == c or n.startswith(c) or c in n:
            return True
    return False


def channel_preference(name: str) -> int:
    """Lower is better. Logo-burn last; Netflix/Toho first; other official next."""
    if is_logo_burn_channel(name):
        return 90
    n = _norm_chan(name)
    for c in PREFERRED_CHANNELS:
        if n == c or n.startswith(c) or c in n:
            return 0
    return 10


def _host(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "", re.I)
    return (m.group(1) or "").lower() if m else ""


def _rank_url(url: str) -> int:
    """Lower = try sooner."""
    h = _host(url)
    if any(h == s or h.endswith("." + s) for s in STUDIO_HOSTS):
        return 10
    if any(h == s or h.endswith("." + s) for s in TRAILER_CLIP_HOSTS):
        return 20
    if "archive.org" in h:
        return 25
    if any(h == s or h.endswith("." + s) for s in PLATFORM_HOSTS):
        return 30
    if any(h == s or h.endswith("." + s) for s in INDIAN_HOSTS):
        return 40
    if any(h == s or h.endswith("." + s) for s in YOUTUBE_HOSTS):
        return 90
    if "github.com" in h and "/releases/download/" in (url or ""):
        return 5  # already-uploaded official fallback: highest priority after primary
    return 50


def load_catalog() -> dict:
    if not CATALOG.exists():
        return {"topics": {}, "official_channels": {}, "clip_queries": []}
    try:
        return json.loads(CATALOG.read_text())
    except Exception:
        return {"topics": {}, "official_channels": {}, "clip_queries": []}


def topic_key(item: dict) -> str:
    """Stable slug from queue id / words / tags."""
    iid = (item.get("id") or "")
    # 20261114-0900-runningman -> runningman
    parts = iid.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        return "-".join(parts[2:]).lower()
    if iid.startswith("backup-"):
        return iid[len("backup-"):].lower()
    w = re.sub(r"[^a-z0-9]+", "", (item.get("words") or "").lower())
    return w or iid.lower()


# Tokens that must never be treated as a topic (the Vinland retry matched
# bluelockisagi_scene1 only because both names share "scene1").
_GENERIC_SOURCE_TOKENS = {
    "scene", "scene1", "scene2", "scene3", "scene4", "scene5", "trailer", "teaser",
    "official", "clip", "clips", "src", "video", "h264", "demo", "stock", "backup",
    "test", "edit", "anime", "movie",
}
_ASSET_TOPIC_RE = re.compile(
    r"^(?P<slug>.+?)(?:_(?:scene\d*|trailer|teaser|official|clip\d*|src))?(?:_h264)?(?:\.[a-z0-9]+)?$",
    re.I,
)


def source_filename(url_or_name: str) -> str:
    return (url_or_name or "").rstrip("/").split("/")[-1].split("?")[0]


def is_trailer_source(url_or_name: str) -> bool:
    """True for trailer/teaser/promo assets or Apple Trailer URLs. Never use these."""
    u = (url_or_name or "").strip()
    if not u:
        return False
    if "trailers.apple.com" in u.lower():
        return True
    return bool(_TRAILER_ASSET.search(source_filename(u)))


def scene_only(urls: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for u in urls or []:
        if u and not is_trailer_source(u) and u not in out:
            out.append(u)
    return out


def item_scene_urls(item: dict) -> list[str]:
    """Known official scene-clip URLs or asset names on a queue/catalog item."""
    urls: list[str] = []
    for u in list(item.get("fallback_mp4") or []) + list(item.get("sources") or []) + list(
        item.get("alt_source_urls") or []
    ) + list(item.get("scene_assets") or []):
        if u and not is_trailer_source(u) and not is_unusable_source(u, item) and u not in urls:
            urls.append(u)
    fa = item.get("fallback_asset") or ""
    if fa and not is_trailer_source(fa) and not is_unusable_source(fa, item) and fa not in urls:
        urls.append(fa)
    return urls


def item_has_scene_source(item: dict) -> bool:
    """True when this item already has at least one official scene clip (not a trailer)."""
    return bool(item_scene_urls(item))


def source_topic_slug(url_or_name: str) -> str:
    """Topic prefix of a sources-release asset, e.g. vinlandsagathorfinn_scene1_h264.mp4."""
    name = source_filename(url_or_name).lower()
    if not name:
        return ""
    m = _ASSET_TOPIC_RE.match(name)
    slug = (m.group("slug") if m else name.rsplit(".", 1)[0]).lower()
    slug = re.sub(r"[^a-z0-9]+", "", slug)
    if slug in _GENERIC_SOURCE_TOKENS or len(slug) < 4:
        return ""
    return slug


def item_topic_slugs(item: dict) -> set[str]:
    """All slugs that identify this item's topic (never generic tokens like scene1)."""
    slugs: set[str] = set()

    def add(raw: str) -> None:
        s = re.sub(r"[^a-z0-9]+", "", (raw or "").lower())
        if len(s) >= 4 and s not in _GENERIC_SOURCE_TOKENS:
            slugs.add(s)

    add(source_topic_slug(item.get("fallback_asset") or ""))
    for u in (item.get("fallback_mp4") or []) + (item.get("sources") or []) + (item.get("alt_source_urls") or []):
        add(source_topic_slug(u))
    st = item.get("stock_topic") or item.get("backup_item") or ""
    if str(st).startswith("backup-"):
        st = st[len("backup-"):]
    add(st)
    add(topic_key(item))
    iid = (item.get("id") or "").lower()
    parts = iid.split("-")
    if len(parts) >= 3 and parts[0].isdigit():
        add("-".join(parts[2:]))
    elif iid.startswith("stock-"):
        add(re.sub(r"-\d{2}$", "", iid[len("stock-"):]))
    elif iid.startswith("demo-clips-"):
        add(iid[len("demo-clips-"):])
    elif iid.startswith("demo-"):
        add(iid[len("demo-"):])
    add(item.get("words") or "")
    return slugs


def unusable_asset_names(item: dict | None = None) -> set[str]:
    """Asset filenames marked unusable on the item or its topic_catalog row."""
    names: set[str] = set()

    def add(obj):
        if not isinstance(obj, dict):
            return
        u = obj.get("unusable_assets")
        if isinstance(u, dict):
            for k in u:
                n = source_filename(k)
                if n:
                    names.add(n)
        elif isinstance(u, list):
            for k in u:
                n = source_filename(str(k))
                if n:
                    names.add(n)
        if obj.get("usable") is False:
            for key in ("scene_assets", "fallback_mp4", "sources", "alt_source_urls"):
                for k in obj.get(key) or []:
                    n = source_filename(str(k))
                    if n and not is_trailer_source(n):
                        names.add(n)
            fa = obj.get("fallback_asset") or ""
            if fa and not is_trailer_source(fa):
                names.add(source_filename(fa))

    add(item)
    try:
        slugs = set()
        if item:
            slugs.add(re.sub(r"[^a-z0-9]+", "", (item.get("slug") or "").lower()))
            slugs |= item_topic_slugs(item)
        slugs.discard("")
        cat = json.loads((REPO / "config" / "topic_catalog.json").read_text())
        for row in cat.get("items") or []:
            rs = re.sub(r"[^a-z0-9]+", "", (row.get("slug") or "").lower())
            if not rs or not slugs:
                continue
            if rs in slugs or any(len(s) >= 5 and (rs.startswith(s) or s.startswith(rs)) for s in slugs):
                add(row)
    except Exception:
        pass
    return names


def _yt_id(url: str) -> str:
    m = re.search(r"(?:v=|/youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else ""


def unusable_urls(item: dict | None = None) -> set[str]:
    """Explicit unusable URLs (e.g. a Crunchyroll YouTube link) from item or catalog."""
    urls: set[str] = set()

    def add(obj):
        if not isinstance(obj, dict):
            return
        for u in obj.get("unusable_urls") or []:
            if u:
                urls.add(str(u).strip())
        ua = obj.get("unusable_assets") or {}
        if isinstance(ua, dict):
            for k in ua:
                if str(k).startswith("http"):
                    urls.add(str(k).strip())

    add(item)
    try:
        slugs = set()
        if item:
            slugs.add(re.sub(r"[^a-z0-9]+", "", (item.get("slug") or "").lower()))
            slugs |= item_topic_slugs(item)
        slugs.discard("")
        cat = json.loads((REPO / "config" / "topic_catalog.json").read_text())
        for row in cat.get("items") or []:
            rs = re.sub(r"[^a-z0-9]+", "", (row.get("slug") or "").lower())
            if not rs or not slugs:
                continue
            if rs in slugs or any(len(s) >= 5 and (rs.startswith(s) or s.startswith(rs)) for s in slugs):
                add(row)
    except Exception:
        pass
    return urls


def is_unusable_source(url_or_name: str, item: dict | None = None) -> bool:
    u = (url_or_name or "").strip()
    if not u:
        return False
    name = source_filename(u)
    if name and name not in ("watch", "shorts") and name in unusable_asset_names(item):
        return True
    yid = _yt_id(u)
    for bad in unusable_urls(item):
        if u == bad or (yid and yid == _yt_id(bad)):
            return True
    return False


def source_matches_topic(item: dict, url_or_name: str) -> bool:
    """True only when this file belongs to the same topic as the queue item."""
    src = source_topic_slug(url_or_name)
    if not src:
        return False
    specific: set[str] = set()
    for u in [item.get("fallback_asset") or ""] + list(item.get("fallback_mp4") or []) + list(item.get("sources") or []):
        s = source_topic_slug(u)
        if s:
            specific.add(s)
    iid = (item.get("id") or "").lower()
    if iid.startswith("demo-clips-"):
        s = re.sub(r"[^a-z0-9]+", "", iid[len("demo-clips-"):])
        if len(s) >= 4:
            specific.add(s)
    elif iid.startswith("stock-"):
        s = re.sub(r"[^a-z0-9]+", "", re.sub(r"-\d{2}$", "", iid[len("stock-"):]))
        if len(s) >= 4:
            specific.add(s)
    st = item.get("stock_topic") or item.get("backup_item") or ""
    if str(st).startswith("backup-"):
        s = re.sub(r"[^a-z0-9]+", "", st[len("backup-"):])
        if len(s) >= 4:
            specific.add(s)
    slugs = specific or item_topic_slugs(item)
    for s in slugs:
        if src == s:
            return True
        if len(s) >= 5 and (src.startswith(s) or s.startswith(src)):
            return True
    return False


def catalog_urls_for(item: dict) -> list[str]:
    """Known official URLs from config/source_catalog.json for this topic."""
    cat = load_catalog()
    topics = cat.get("topics") or {}
    key = topic_key(item)
    row = topics.get(key) or {}
    # also try tag / words matches
    if not row:
        for t in item.get("tags") or []:
            if t in topics:
                row = topics[t]
                break
    urls: list[str] = []
    for field in ("studio_urls", "press_kit_urls", "clip_urls", "scene_urls",
                  "platform_urls", "indian_urls", "archive_urls", "youtube_urls",
                  "alt_source_urls", "urls"):
        for u in row.get(field) or []:
            if u and u not in urls and not is_trailer_source(u):
                urls.append(u)
    return scene_only(urls)


def search_queries(item: dict) -> list[str]:
    """yt-dlp search strings across platforms (official phrasing only)."""
    film = (item.get("words") or "").strip() or topic_key(item).replace("-", " ")
    kind = (item.get("content_kind") or "best scene").lower()
    if kind in ("trailer", "teaser", ""):
        kind = "best scene"
    pillar = (item.get("pillar") or "movie").lower()
    queries = []
    scene_kinds = [kind] if "scene" in kind or "clip" in kind else [
        "best scene", "action scene", "fight scene", "emotional scene",
    ]
    if pillar == "romance":
        scene_kinds = ["romance scene", "love scene", "emotional scene"] + scene_kinds
    if pillar == "anime":
        scene_kinds = ["best fight", "anime clip", "official clip"] + scene_kinds
    for sk in scene_kinds[:4]:
        queries.append(f"{film} {sk} official")
    # platform-scoped searches (yt-dlp prefixes) — never "trailer" / "teaser"
    bases = [f"{film} official clip", f"{film} best scene official"]
    out = list(queries)
    for b in bases:
        out += [
            f"ytsearch3:{b}",
            f"dmsearch3:{b}",
            f"bilibilisearch2:{b}",
        ]
    # de-dupe preserve order
    seen = set()
    uniq = []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:16]


def build_candidate_urls(item: dict, *, include_youtube: bool = True) -> list[str]:
    """Ordered unique URL list for one queue item."""
    urls: list[str] = []

    def add(u: str | None):
        u = (u or "").strip()
        if not u or u in urls or is_unusable_source(u, item):
            return
        urls.append(u)

    # 1) Already-hosted GitHub release fallbacks (never need YouTube)
    for u in item.get("fallback_mp4") or []:
        add(u)
    # 2) Explicit alt_source_urls on the item
    for u in item.get("alt_source_urls") or []:
        add(u)
    # 3) Catalog (studio -> clips -> platforms -> indian -> youtube)
    for u in catalog_urls_for(item):
        add(u)
    # 4) Primary source_url only when it is not a trailer/teaser
    su = item.get("source_url")
    kind = (item.get("content_kind") or "").lower()
    if su and not is_trailer_source(su) and kind not in ("trailer", "teaser"):
        add(su)

    # Sort non-github by host rank, but keep github fallbacks first.
    # Trailers are never candidates, even as a last fallback.
    urls = scene_only(urls)
    gh, rest = [], []
    for u in urls:
        if "github.com" in u and "/releases/download/" in u:
            gh.append(u)
        else:
            rest.append(u)
    rest.sort(key=_rank_url)
    if not include_youtube:
        rest = [u for u in rest if _rank_url(u) < 90]
    return gh + rest


def _ok_src(p: Path, min_bytes: int = 500_000) -> bool:
    return p.exists() and p.stat().st_size > min_bytes


def _probe_height(path: Path) -> int:
    try:
        o = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", str(path)],
            text=True, timeout=30,
        ).strip()
        return int(o.split(",")[0] or 0)
    except Exception:
        return 0


def download_direct(url: str, dest: Path) -> tuple[bool, str]:
    """Fetch a direct mp4 (GitHub release via gh, else curl)."""
    dest.unlink(missing_ok=True)
    if "github.com" in url and "/releases/download/" in url:
        parts = url.split("/releases/download/")
        repo = parts[0].split("github.com/")[-1].strip("/")
        tag, asset = parts[1].split("/", 1)
        r = subprocess.run(
            ["gh", "release", "download", tag, "-p", asset, "-R", repo,
             "-D", str(dest.parent), "--clobber"],
            capture_output=True, text=True,
        )
        cand = dest.parent / asset
        if r.returncode == 0 and _ok_src(cand):
            if cand.resolve() != dest.resolve():
                dest.write_bytes(cand.read_bytes())
            return True, f"gh:{url}"
        return False, f"gh:{asset}:{(r.stderr or '')[-160:]}"
    r = subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "-L", "-o", str(dest), url],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and _ok_src(dest):
        return True, url
    return False, f"curl:{url}:{(r.stderr or '')[-160:]}"


def _cookies_args() -> list[str]:
    """Optional download-only Google account cookies from secret path.

    Never use the Skywalker channel account. Owner may later set
    YTDLP_COOKIES_FILE (or YTDLP_COOKIES env contents written by the workflow)
    for a separate download-only Google account.
    """
    path = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip()
    if path and Path(path).exists():
        return ["--cookies", path]
    return []


def download_ytdlp(url: str, dest: Path, *, allow_youtube: bool = True) -> tuple[bool, str]:
    if not allow_youtube and any(h in _host(url) for h in ("youtube.com", "youtu.be")):
        return False, "youtube skipped (blocked / no cookies)"
    dest.unlink(missing_ok=True)
    cmd = [
        "yt-dlp", "--no-playlist", "--remote-components", "ejs:github",
        "-4", "--user-agent", UA,
        "-f", YTDLP_FORMAT,
        "--merge-output-format", "mp4",
        "-o", str(dest), "--force-overwrites",
        "--no-warnings",
    ] + _cookies_args() + [url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if r.returncode == 0 and _ok_src(dest):
        h = _probe_height(dest)
        if h and h < 720:
            dest.unlink(missing_ok=True)
            return False, f"yt-dlp:{url}: height {h}<720"
        return True, url
    err = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["?"])[-1][:280]
    return False, f"yt-dlp:{err}"


def download_source(
    item: dict,
    dest: Path,
    *,
    force_fallback: bool = False,
    skip: set[str] | None = None,
    sibling_urls: list[str] | None = None,
    allow_youtube: bool = True,
    on_try: Callable[[str], None] | None = None,
) -> tuple[str | None, list[str]]:
    """Try every official candidate until one lands a 720p+ mp4.

    Order: GitHub sources release / fallback_mp4 -> catalog studio/press ->
    trailer sites -> platforms (DM/Vimeo/Archive/...) -> Indian free clips ->
    item alt URLs -> sibling release assets -> YouTube last.
    """
    errors: list[str] = []
    skip = set(skip or [])

    def skipped(info: str) -> bool:
        if info in skip:
            return True
        key = info.rstrip("/").split("/")[-1].split("?")[0].lower()
        return any(key and key == s.rstrip("/").split("/")[-1].split("?")[0].lower() for s in skip)

    candidates = build_candidate_urls(item, include_youtube=allow_youtube)
    for u in sibling_urls or []:
        if u and u not in candidates:
            candidates.append(u)

    if force_fallback:
        # Prefer non-YouTube / release assets first
        candidates = [u for u in candidates if _rank_url(u) < 90] + [
            u for u in candidates if _rank_url(u) >= 90
        ]

    for u in candidates:
        if not u or skipped(u) or skipped(f"gh:{u}"):
            continue
        if is_trailer_source(u):
            errors.append(f"skip trailer: {u}")
            continue
        if is_unusable_source(u, item):
            errors.append(f"skip unusable: {u}")
            continue
        if on_try:
            on_try(u)
        is_direct = (
            u.lower().endswith(".mp4")
            or ("github.com" in u and "/releases/download/" in u)
            or "archive.org/download/" in u
        )
        if is_direct:
            ok, info = download_direct(u, dest)
            if ok and not skipped(info):
                return info, errors
            errors.append(info)
            # archive.org pages sometimes need yt-dlp
            if "archive.org" in u and not u.lower().endswith(".mp4"):
                ok, info = download_ytdlp(u, dest, allow_youtube=False)
                if ok:
                    return info, errors
                errors.append(info)
            continue
        # Hosted pages: yt-dlp (YouTube last / optional)
        is_yt = any(h in _host(u) for h in ("youtube.com", "youtu.be"))
        ok, info = download_ytdlp(u, dest, allow_youtube=allow_youtube or not is_yt)
        if ok and not skipped(info):
            return info, errors
        errors.append(info)
        time.sleep(1)

    return None, errors


def clip_search_urls(item: dict, limit: int = 6) -> list[str]:
    """Best-effort discover official clip URLs via yt-dlp flat search (no download)."""
    found: list[str] = []
    for q in search_queries(item):
        if not q.startswith(("ytsearch", "dmsearch", "bilibilisearch")):
            q = f"ytsearch2:{q}"
        try:
            r = subprocess.run(
                ["yt-dlp", "--flat-playlist", "-J", "--no-warnings", q],
                capture_output=True, text=True, timeout=45,
            )
            if r.returncode != 0:
                continue
            for e in (json.loads(r.stdout).get("entries") or []):
                title = e.get("title") or ""
                if _BAD_TITLE.search(title) or _WATERMARK_HINT.search(title):
                    continue
                url = e.get("url") or e.get("webpage_url")
                if not url and e.get("id"):
                    # reconstruct
                    if "dmsearch" in q:
                        url = f"https://www.dailymotion.com/video/{e['id']}"
                    else:
                        url = f"https://www.youtube.com/watch?v={e['id']}"
                if url and url not in found:
                    found.append(url)
                if len(found) >= limit:
                    return found
        except Exception:
            continue
    return found


def apple_trailer_search_url(film: str) -> str:
    """Apple Trailers search page (yt-dlp can resolve some Apple trailer feeds)."""
    return f"https://trailers.apple.com/trailers/home/scripts/quickfind.php?q={quote_plus(film)}"


def is_trailer_result(r: dict | None) -> bool:
    """True when a render result (or stock entry) was built from a trailer/teaser source."""
    if not r:
        return False
    for key in ("source", "fallback_asset", "asset", "used_source"):
        val = r.get(key)
        if val and is_trailer_source(str(val)):
            return True
    for key in ("fallback_mp4", "sources", "scene_assets", "alt_source_urls"):
        for u in r.get(key) or []:
            if u and is_trailer_source(str(u)):
                # A mixed list is still usable if a scene clip exists; only flag
                # when the *used* source is missing and every listed source is a trailer.
                pass
    # asset_url of the *output* is never a trailer (it's the Short). Ignore it.
    return False


def is_unrecoverable_source_error(err: str | None) -> bool:
    """True when retrying the same topic cannot produce a source (404, rejected clips, YT bot-check).

    Transient network/5xx/timeout errors are recoverable and must still be retried.
    """
    e = (err or "").lower()
    if not e:
        return False
    transient = (
        "timed out", "timeout", "503", "502", "500", "429",
        "temporarily", "connection reset", "connection aborted",
        "network is unreachable", "ssl", "eof",
    )
    if any(w in e for w in transient):
        return False
    markers = (
        "http error 404", "404: not found", "error 404",
        "no official scene clips",
        "no unused official source",
        "reject burned-in", "reject logo", "reject logo/watermark",
        "sign in to confirm",
        "skip trailer",
    )
    return any(w in e for w in markers)

