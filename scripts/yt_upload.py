#!/usr/bin/env python3
"""Publish passing renders straight to YouTube (Skywalker) with the YouTube Data API v3.

Used by publish.yml instead of Buffer when the SKY_YT_CLIENT_ID / SKY_YT_CLIENT_SECRET /
SKY_YT_REFRESH_TOKEN secrets exist (Buffer's free plan caps Shorts at 2 per 24h).

- OAuth: refresh-token grant -> short-lived access token (never printed).
- Upload: resumable videos.insert. Slot more than PUBLISH_AT_LEAD_MIN away -> uploaded
  private with status.publishAt = slot (YouTube flips it public at the slot); otherwise
  uploaded public now. Always selfDeclaredMadeForKids=False, category from config.
- Same eligibility, metadata gate, repeat guard and late-slot rules as buffer_post.py.
- Ledger: results/<id>.json gets yt_* fields and the shared publish fields every other
  script reads (buffer_post_id="yt:<videoId>", buffer_status scheduled/sent,
  buffer_due_at, youtube_url), so slot-guardian / health / backup stock see it as covered.
- Quota / rate-limit errors re-queue the result (yt_status=quota_wait, never dead) and stop
  uploading for this run. Other upload errors retry on the next run; a second failure
  marks the result publish-dead (render stays done, nothing is deleted).

Env: SKY_YT_CLIENT_ID, SKY_YT_CLIENT_SECRET, SKY_YT_REFRESH_TOKEN (secrets; never printed).
Optional BUFFER_API_KEY (only read, for the repeat guard). DRY_RUN=1, IDS=comma list.
`--auth-check` prints only the channel title and handle.
"""
import glob, json, os, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import backup_stock  # noqa: E402
import buffer_post as bp  # noqa: E402

IST = bp.IST
CFG = bp.CFG
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SECRET_ENVS = ("SKY_YT_CLIENT_ID", "SKY_YT_CLIENT_SECRET", "SKY_YT_REFRESH_TOKEN")
PUBLISH_AT_LEAD_MIN = 15
CHUNK = 8 * 1024 * 1024  # multiple of 256 KiB
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded",
                 "userRateLimitExceeded", "rateLimitExceededUnreg"}
MAX_FAILS = 2
RESTRICT_GRACE_MIN = 30


class QuotaError(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


def yt_configured(env=None):
    env = os.environ if env is None else env
    return all((env.get(k) or "").strip() for k in SECRET_ENVS)


# ---------------------------------------------------------------- HTTP / auth

def _err_reasons(body):
    try:
        e = json.loads(body).get("error") or {}
    except Exception:
        return set(), (body or "")[:300]
    if isinstance(e, str):  # oauth endpoint: {"error": "invalid_grant", ...}
        return {e}, e
    reasons = {x.get("reason") for x in (e.get("errors") or []) if x.get("reason")}
    st = e.get("status")
    if st:
        reasons.add(st)
    return reasons, str(e.get("message") or "")[:300]


def classify_http_error(code, body):
    """Raise QuotaError for quota/rate limits, AuthError for auth, RuntimeError otherwise."""
    reasons, msg = _err_reasons(body)
    if code == 429 or reasons & QUOTA_REASONS or "RESOURCE_EXHAUSTED" in reasons:
        raise QuotaError(f"YouTube quota/rate limit (HTTP {code} {','.join(sorted(reasons))}): {msg}")
    if code == 401 or reasons & {"invalid_grant", "invalid_client", "unauthorized_client", "authError"}:
        raise AuthError(f"YouTube auth error (HTTP {code} {','.join(sorted(reasons))}): {msg}")
    raise RuntimeError(f"YouTube HTTP {code} {','.join(sorted(reasons))}: {msg}")


def _open(req, timeout=120):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def access_token():
    if not yt_configured():
        raise AuthError("SKY_YT_* secrets are not set")
    data = urllib.parse.urlencode({
        "client_id": os.environ["SKY_YT_CLIENT_ID"].strip(),
        "client_secret": os.environ["SKY_YT_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["SKY_YT_REFRESH_TOKEN"].strip(),
        "grant_type": "refresh_token",
    }).encode()
    code, _, body = _open(urllib.request.Request(TOKEN_URL, data=data), timeout=60)
    if code != 200:
        reasons, msg = _err_reasons(body.decode(errors="replace"))
        raise AuthError(f"token refresh failed (HTTP {code} {','.join(sorted(reasons))}) {msg}")
    tok = json.loads(body).get("access_token")
    if not tok:
        raise AuthError("token refresh returned no access_token")
    return tok


def api_get(tok, path, params):
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    code, _, body = _open(urllib.request.Request(url, headers={"Authorization": "Bearer " + tok}), 60)
    if code != 200:
        classify_http_error(code, body.decode(errors="replace"))
    return json.loads(body)


def my_channel(tok):
    d = api_get(tok, "channels", {"part": "snippet,contentDetails", "mine": "true"})
    items = d.get("items") or []
    if not items:
        raise AuthError("token has no YouTube channel")
    return items[0]


def channel_uploads(tok, ch, n=50):
    """Recent uploads (incl. private/scheduled ones, owner token) as Buffer-like post dicts."""
    pl = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not pl:
        return []
    d = api_get(tok, "playlistItems", {"part": "snippet", "playlistId": pl, "maxResults": str(n)})
    out = []
    for it in d.get("items") or []:
        sn = it.get("snippet") or {}
        vid = (sn.get("resourceId") or {}).get("videoId")
        if vid:
            out.append({"id": "yt:" + vid, "status": "sent", "sentAt": sn.get("publishedAt"),
                        "metadata": {"title": sn.get("title") or ""}})
    return out


# ---------------------------------------------------------------- metadata

def _clean(s):
    return (s or "").replace("<", "").replace(">", "")


def yt_tags(tags, limit=480):
    if isinstance(tags, str):
        try:
            tags = json.loads(tags.replace("'", '"'))
        except Exception:
            tags = [t for t in tags.replace(",", " ").split() if t]
    out, total, seen = [], 0, set()
    for t in tags or []:
        t = _clean(str(t)).strip().lstrip("#").strip().replace('"', "")
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > limit:
            break
        out.append(t); seen.add(t.lower()); total += cost
    return out


def video_body(r, publish_at=None):
    title = _clean(r.get("title") or "").strip()[:100]
    desc = _clean(r.get("description_final") or r.get("description") or title)
    while len(desc.encode()) > 4900:
        desc = desc[:-50]
    status = {"selfDeclaredMadeForKids": False, "embeddable": True, "license": "youtube",
              "publicStatsViewable": True}
    if publish_at is not None:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        status["privacyStatus"] = "public"
    return {"snippet": {"title": title, "description": desc, "tags": yt_tags(r.get("tags")),
                        "categoryId": str(CFG.get("youtube_category_id") or "24"),
                        "defaultLanguage": "en"},
            "status": status}


def schedule_for(slot, now, lead_min=PUBLISH_AT_LEAD_MIN):
    """publishAt for a slot, or None to publish right away (slot due / too close)."""
    return slot if slot > now + timedelta(minutes=lead_min) else None


# ---------------------------------------------------------------- upload

def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "skywalker-publish"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    if os.path.getsize(dest) < 10_000:
        raise RuntimeError("downloaded asset is too small")
    return dest


def _uploaded_bytes(headers):
    rng = headers.get("Range") or headers.get("range") or ""
    if rng.startswith("bytes=") and "-" in rng:
        return int(rng.split("-")[-1]) + 1
    return 0


def resumable_upload(tok, path, body, notify=True):
    size = os.path.getsize(path)
    q = urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status",
                                "notifySubscribers": "true" if notify else "false"})
    req = urllib.request.Request(f"{UPLOAD_URL}?{q}", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
    code, hdr, raw = _open(req, 60)
    if code != 200:
        classify_http_error(code, raw.decode(errors="replace"))
    session = hdr.get("Location") or hdr.get("location")
    if not session:
        raise RuntimeError("resumable upload: no session URL")
    pos, tries = 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(pos)
            chunk = f.read(CHUNK)
            end = pos + len(chunk) - 1
            put = urllib.request.Request(session, data=chunk, method="PUT", headers={
                "Authorization": "Bearer " + tok, "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {pos}-{end}/{size}"})
            try:
                code, hdr, raw = _open(put, 300)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                code, hdr, raw = 0, {}, str(e).encode()
            if code in (200, 201):
                return json.loads(raw)
            if code == 308:
                pos, tries = _uploaded_bytes(hdr), 0
                continue
            if code in (0, 500, 502, 503, 504) and tries < 5:
                tries += 1
                time.sleep(2 ** tries)
                st = urllib.request.Request(session, data=b"", method="PUT", headers={
                    "Authorization": "Bearer " + tok, "Content-Length": "0", "Content-Range": f"bytes */{size}"})
                c2, h2, r2 = _open(st, 60)
                if c2 in (200, 201):
                    return json.loads(r2)
                if c2 == 308:
                    pos = _uploaded_bytes(h2)
                continue
            classify_http_error(code, raw.decode(errors="replace"))


# ---------------------------------------------------------------- ledger

def record_upload(r, video, publish_at, now):
    vid = video["id"]
    st = (video.get("status") or {})
    when = publish_at or now
    r.pop("buffer_error", None)
    r.update(publish_via="youtube", yt_video_id=vid, yt_uploaded_at=now.strftime("%Y-%m-%d %H:%M IST"),
             yt_privacy=st.get("privacyStatus"), yt_publish_at=when.isoformat(),
             yt_status="scheduled" if publish_at else "public",
             youtube_url=f"https://youtube.com/shorts/{vid}",
             buffer_post_id="yt:" + vid, buffer_status="scheduled" if publish_at else "sent",
             buffer_due_at=when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    if not publish_at:
        r["buffer_posted_at"] = r["buffer_due_at"]
    for k in ("yt_error", "yt_quota_hit_at"):
        r.pop(k, None)
    return r


def move_failed_buffer_post(r):
    """A Buffer post that errored / never existed is replaced by a direct upload."""
    pid = r.get("buffer_post_id") or ""
    if pid and not pid.startswith("yt:"):
        r["buffer_prev_post_id"] = pid
    for k in ("buffer_post_id", "buffer_status", "buffer_due_at"):
        r.pop(k, None)


def already_published(r):
    """Uploaded here, or queued/sent through Buffer (left to Buffer): never post twice."""
    if r.get("yt_video_id"):
        return True
    return bool(r.get("buffer_post_id")) and r.get("buffer_status") in ("sent", "scheduled", "posted")


def mark_quota_wait(r, err, now):
    r.update(yt_status="quota_wait", yt_error=str(err)[:300], limit_deferred=True,
             yt_quota_hit_at=now.strftime("%Y-%m-%d %H:%M IST"))
    return r


def mark_failed(r, err, now):
    """Non-quota upload failure: retry next run; MAX_FAILS-th failure -> publish dead."""
    n = int(r.get("yt_fail_count") or 0) + 1
    r.update(yt_fail_count=n, yt_error=str(err)[:300], yt_status="error")
    if n >= MAX_FAILS:
        bp.mark_publish_dead(r, f"YouTube upload failed {n}x: {err}", now)
        return "dead"
    return "retry"


def refresh_status(tok, rows, now, changed):
    """Flip scheduled uploads to sent once public; flag ones YouTube kept private."""
    def pending(r):
        if r.get("publish_via") != "youtube" or not r.get("yt_video_id"):
            return False
        return r.get("yt_status") == "scheduled" or (r.get("yt_status") == "public"
                                                     and not r.get("yt_checked_public"))
    want = [(p, r) for p, r in rows if pending(r)]
    warns = []
    for i in range(0, len(want), 50):
        part = want[i:i + 50]
        d = api_get(tok, "videos", {"part": "status", "id": ",".join(r["yt_video_id"] for _, r in part)})
        found = {v["id"]: v.get("status") or {} for v in d.get("items") or []}
        for p, r in part:
            st = found.get(r["yt_video_id"])
            upd = {}
            if st is None:
                upd = {"yt_status": "missing"}
                warns.append(f"{r['id']}: video {r['yt_video_id']} not found on the channel")
            elif st.get("uploadStatus") in ("rejected", "failed"):
                upd = {"yt_status": "rejected", "yt_error": f"{st.get('uploadStatus')}: "
                       f"{st.get('rejectionReason') or st.get('failureReason')}"}
                warns.append(f"{r['id']}: YouTube {upd['yt_error']}")
            elif st.get("privacyStatus") == "public":
                upd = {"yt_status": "public", "yt_privacy": "public", "buffer_status": "sent",
                       "yt_checked_public": True,
                       "buffer_posted_at": r.get("buffer_due_at")}
            else:
                pa = bp._parse_dt(r.get("yt_publish_at"))
                if pa and now > pa + timedelta(minutes=RESTRICT_GRACE_MIN) and not r.get("yt_restricted"):
                    upd = {"yt_restricted": True, "yt_privacy": st.get("privacyStatus")}
                    warns.append(f"{r['id']}: still {st.get('privacyStatus')} {RESTRICT_GRACE_MIN}+ min after its "
                                 "publish time - YouTube may be locking API uploads from an unaudited "
                                 "project to private")
            if upd and any(r.get(k) != v for k, v in upd.items()):
                r.update(upd); bp.save(p, r); changed.append(r["id"])
    return warns


# ---------------------------------------------------------------- main

def eligible(r, start, all_r):
    if r.get("status") != "done" or not r.get("asset_url") or not r.get("slot"):
        return False
    if backup_stock.must_not_publish(r):
        return False
    if (r.get("id") or "").startswith("stock-") and not r.get("assigned_from_stock"):
        return False
    if bp.is_publish_dead(r):
        return False
    try:
        if datetime.fromisoformat(r["slot"]) < start:
            return False
    except Exception:
        return False
    return True


def auth_check():
    tok = access_token()
    ch = my_channel(tok)
    sn = ch.get("snippet") or {}
    print(f"channel: {sn.get('title')}")
    print(f"handle: {sn.get('customUrl') or '(none)'}")
    return 0


def video_status(ids):
    """Print privacy / publishAt / upload + processing status for our own video ids (no secrets)."""
    ids = [x.strip() for x in ids if x.strip()]
    if not ids:
        print("no video ids")
        return 1
    tok = access_token()
    d = api_get(tok, "videos", {"part": "status,processingDetails,snippet", "id": ",".join(ids[:50])})
    found = {v["id"]: v for v in d.get("items") or []}
    for vid in ids:
        v = found.get(vid)
        if not v:
            print(f"{vid}: not found")
            continue
        st, pd = v.get("status") or {}, v.get("processingDetails") or {}
        print(f"{vid}: title={(v.get('snippet') or {}).get('title')!r} privacy={st.get('privacyStatus')} "
              f"publishAt={st.get('publishAt')} upload={st.get('uploadStatus')} "
              f"rejection={st.get('rejectionReason')} failure={st.get('failureReason')} "
              f"madeForKids={st.get('madeForKids')} processing={pd.get('processingStatus')} "
              f"processingFailure={pd.get('processingFailureReason')}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--auth-check" in argv:
        return auth_check()
    if "--status" in argv:
        return video_status(os.environ.get("VIDEO_IDS", "").split(","))
    dry = os.environ.get("DRY_RUN") == "1"
    only = {x.strip() for x in os.environ.get("IDS", "").split(",") if x.strip()}
    now = datetime.now(IST)
    start = datetime.fromisoformat(CFG["post_from_slot"])
    late_h = float(CFG.get("late_post_hours") or 6)
    limit_defer_h = float(CFG.get("limit_defer_hours") or 12)
    rows = []
    for p in sorted(glob.glob(str(REPO / "results" / "*.json"))):
        try:
            rows.append((p, json.loads(Path(p).read_text())))
        except Exception:
            continue
    all_r = [r for _, r in rows]
    failed, changed, warns = [], [], []
    try:
        tok = access_token()
        ch = my_channel(tok)
    except AuthError as e:
        print(f"::error::{e}")
        return 1
    print(f"YouTube direct upload -> {(ch.get('snippet') or {}).get('title')}")
    warns += refresh_status(tok, rows, now, changed)
    posts = channel_uploads(tok, ch)
    if os.environ.get("BUFFER_API_KEY"):
        try:
            posts += bp.channel_posts()
        except Exception as e:
            print(f"(Buffer posts not read for the repeat guard: {str(e)[:120]})")
    idx = bp.posted_index(all_r, posts)
    quota_hit = None
    for p, r in rows:
        iid = r.get("id")
        if only and iid not in only:
            continue
        if not eligible(r, start, all_r):
            continue
        if already_published(r):
            continue
        if backup_stock.slot_post_conflicts(r, all_r):
            print(f"{iid}: slot already covered by another result; not posting")
            continue
        why = bp.repeat_of(r, idx)
        if why:
            bp.block_repeat(p, r, why, now, changed)
            idx[:] = [e for e in idx if e["id"] != iid]
            continue
        errs = bp.metadata_errors(r)
        if errs:
            print(f"{iid}: NOT posting ({'; '.join(errs)})")
            continue
        dup = next((x for x in posts if (x.get("status") or "") in ("sent", "scheduled") and bp.norm((x.get("metadata") or {}).get("title")) == bp.norm(r["title"])), None)
        if dup:
            print(f"{iid}: same title already on the channel ({dup['id']}); not uploading again")
            if not dry:
                r.update(publish_blocked=f"repeat: title already on channel ({dup['id']})"[:300],
                         publish_blocked_at=now.strftime("%Y-%m-%d %H:%M IST"))
                bp.save(p, r); changed.append(iid)
            continue
        slot = datetime.fromisoformat(r["slot"])
        win_h = limit_defer_h if r.get("limit_deferred") else late_h
        if slot < now - timedelta(hours=win_h):
            print(f"{iid}: slot {r['slot']} is more than {win_h}h past and not posted - needs a manual decision")
            failed.append(f"{iid}: missed slot not auto-posted (>{win_h}h late)")
            continue
        if quota_hit:
            if not dry:
                mark_quota_wait(r, quota_hit, now); bp.save(p, r); changed.append(iid)
            continue
        publish_at = schedule_for(slot, now)
        how = f"private, publishAt {slot:%Y-%m-%d %H:%M IST}" if publish_at else "public now"
        print(f"{iid}: upload ({how}): {r['title']!r}")
        if dry:
            continue
        try:
            with tempfile.TemporaryDirectory() as td:
                f = download(r["asset_url"], os.path.join(td, "v.mp4"))
                video = resumable_upload(tok, f, video_body(r, publish_at))
        except QuotaError as e:
            quota_hit = str(e)
            warns.append(f"{iid}: {e} - re-queued, will retry next run")
            mark_quota_wait(r, e, now); bp.save(p, r); changed.append(iid)
            continue
        except AuthError as e:
            failed.append(f"{iid}: {e}")
            break
        except Exception as e:
            what = mark_failed(r, e, now)
            failed.append(f"{iid}: upload failed ({what}): {e}")
            bp.save(p, r); changed.append(iid)
            continue
        move_failed_buffer_post(r)
        record_upload(r, video, publish_at, now)
        bp.save(p, r); changed.append(iid)
        posts.append({"id": "yt:" + video["id"], "status": "sent", "metadata": {"title": r["title"]}})
        idx.append({"key": backup_stock.hook_key(r["title"]), "when": slot, "id": iid, "post": "yt:" + video["id"],
                    "sent": False, "source": str(r.get("source") or "")})
        print(f"{iid}: uploaded {r['youtube_url']} ({r['yt_status']})")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        open(out, "a").write(f"changed={','.join(dict.fromkeys(changed))}\n")
    for w in warns:
        print("::warning::" + w[:900])
    if failed:
        print("::error::" + " | ".join(failed)[:900])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
