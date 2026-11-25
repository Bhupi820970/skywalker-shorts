#!/usr/bin/env python3
"""Post passing renders to YouTube (Skywalker) through the Buffer GraphQL API.

Idempotent: a result with buffer_post_id is never posted again, and before posting we look
for an existing Buffer post on the channel with the same title (also catches manual Studio
uploads that Buffer synced). Writes buffer_* fields back into results/<id>.json.

A Buffer post that YouTube rejects (status=error) is retried once: same slot if it is
still due (or within late_post_hours), otherwise the next free gap. If the retry also
errors, the result is marked buffer_status=dead so later publish runs skip it and stay
green. Render status stays done (never re-render, never delete YouTube videos).

Env: BUFFER_API_KEY (secret; never printed). DRY_RUN=1 prints what would be posted.
IDS=comma list limits to those ids. FORCE=1 skips the late-slot guard only for ids listed in IDS.
Exit 1 if a createPost failed for a new post.
"""
import glob, json, os, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import backup_stock  # noqa: E402
import posted_history  # noqa: E402
import hook_text  # noqa: E402
import sky_ai  # noqa: E402
import slot_times  # noqa: E402

API = "https://api.buffer.com"
IST = timezone(timedelta(hours=5, minutes=30))
CFG = json.loads((REPO / "config" / "publish.json").read_text())
CH, ORG = CFG["buffer_channel_id"], CFG["buffer_org_id"]
DRY = os.environ.get("DRY_RUN") == "1"
ONLY = {x.strip() for x in os.environ.get("IDS", "").split(",") if x.strip()}
FORCE = os.environ.get("FORCE") == "1"


def gql(query, variables=None):
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        raise SystemExit("BUFFER_API_KEY secret is not set")
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Buffer HTTP {e.code}: {e.read().decode()[:300]}")
    if d.get("errors"):
        raise RuntimeError("Buffer GraphQL error: " + "; ".join(x.get("message", "?") for x in d["errors"])[:400])
    return d["data"]


def channel_posts():
    q = """query P($org: OrganizationId!, $ch: [ChannelId!]) {
      posts(first: 100, input: {organizationId: $org, filter: {channelIds: $ch}}) {
        edges { node { id status dueAt sentAt externalLink error { message }
          metadata { __typename ... on YoutubePostMetadata { title } } } } } }"""
    d = gql(q, {"org": ORG, "ch": [CH]})
    return [e["node"] for e in d["posts"]["edges"]]


def limit_status(when):
    """Buffer's own daily posting limit check for a post at `when` (datetime)."""
    q = """query L($ch: [ChannelId!]!, $d: DateTime) {
      dailyPostingLimits(input: {channelIds: $ch, date: $d}) { channelId isAtLimit limit scheduled sent } }"""
    d = gql(q, {"ch": [CH], "d": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    rows = d.get("dailyPostingLimits") or []
    return rows[0] if rows else {}


def delete_post(post_id):
    """Delete a queued (never a sent) Buffer post. Caller checks status != sent."""
    m = """mutation D($input: DeletePostInput!) { deletePost(input: $input) {
      ... on DeletePostSuccess { id }
      ... on MutationError { message } } }"""
    d = gql(m, {"input": {"id": post_id}})["deletePost"]
    if not d.get("id"):
        raise RuntimeError("deletePost: " + str(d.get("message")))
    return d["id"]


def norm(t):
    return " ".join((t or "").lower().split())


def video_asset(r):
    """Buffer video asset. Do not set thumbnailUrl — the API rejects it, and
    YouTube Shorts have no custom thumbnail via Buffer. The Short's cover is
    the video's first frame (a dramatic poster written by cover.py).
    """
    asset = {"video": {"url": r["asset_url"]}}
    return asset


def post_input(r, due=None, share_now=False):
    title = (r.get("title") or "")[:100]
    text = r.get("description_final") or r.get("description") or title
    inp = {
        "text": text[:4900],
        "channelId": CH,
        "schedulingType": "automatic",
        "mode": "shareNow" if share_now else "customScheduled",
        "assets": [video_asset(r)],
        "metadata": {"youtube": {"title": title, "privacy": "public", "madeForKids": False,
                                 "categoryId": str(CFG.get("youtube_category_id") or "24"),
                                 "notifySubscribers": True, "embeddable": True}},
        "source": "skywalker-shorts",
    }
    if not share_now and due is not None:
        inp["dueAt"] = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return inp


def create(r, due, share_now):
    m = """mutation C($input: CreatePostInput!) { createPost(input: $input) {
      ... on PostActionSuccess { post { id status dueAt } }
      ... on MutationError { message } } }"""
    inp = post_input(r, due, share_now)
    d = gql(m, {"input": inp})["createPost"]
    if not d.get("post"):
        raise RuntimeError("createPost: " + str(d.get("message")))
    return d["post"]


def edit(r, post_id, due_at):
    m = """mutation E($input: EditPostInput!) { editPost(input: $input) {
      ... on PostActionSuccess { post { id status dueAt } }
      ... on MutationError { message } } }"""
    inp = {"id": post_id, "text": (r.get("description_final") or r.get("description") or r["title"])[:4900],
           "assets": [video_asset(r)],
           "metadata": {"youtube": {"title": r["title"][:100], "privacy": "public", "madeForKids": False,
                                    "categoryId": str(CFG.get("youtube_category_id") or "24"),
                                    "notifySubscribers": True, "embeddable": True}}}
    if due_at:
        inp["dueAt"] = due_at
        inp["mode"] = "customScheduled"
        inp["schedulingType"] = "automatic"
    d = gql(m, {"input": inp})["editPost"]
    if not d.get("post"):
        raise RuntimeError("editPost: " + str(d.get("message")))
    return d["post"]


def save(p, r):
    Path(p).write_text(json.dumps(r, indent=2, ensure_ascii=False))


def is_publish_dead(r):
    return (r.get("buffer_status") == "dead" or r.get("publish_dead") or r.get("status") == "dead"
            or bool(r.get("publish_blocked")))


def mark_publish_dead(r, note, now):
    """Keep render status=done (no re-render, no delete). Later publish runs skip this."""
    r["publish_dead"] = True
    r["buffer_status"] = "dead"
    r["buffer_note"] = (note or "Buffer error after retry")[:600]
    r["buffer_dead_at"] = now.strftime("%Y-%m-%d %H:%M IST")
    return r


def _parse_dt(s):
    if not s:
        return None
    try:
        raw = str(s).replace(" IST", "").replace("Z", "+00:00")
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except Exception:
        return None


def occupied_times(all_r, posts, now):
    """IST datetimes already taken by a scheduled/sent post, a ready result, or a queued item."""
    taken = []
    for r in all_r:
        if backup_stock.must_not_publish(r) or is_publish_dead(r):
            continue
        if backup_stock.is_title_match_miss(r):
            continue
        st = r.get("buffer_status")
        if st in ("sent", "scheduled") or backup_stock.is_ready_result(r):
            dt = _parse_dt(r.get("buffer_due_at") if st in ("sent", "scheduled") else None) or _parse_dt(r.get("slot"))
            if dt:
                taken.append(dt)
    for x in posts or []:
        if (x.get("status") or "") not in ("sent", "scheduled"):
            continue
        dt = _parse_dt(x.get("dueAt") or "") or _parse_dt(x.get("sentAt") or "")
        if dt:
            taken.append(dt)
    qdir = REPO / "queue"
    if qdir.exists():
        for f in sorted(qdir.glob("2026-*.json")):
            try:
                items = json.loads(f.read_text())
            except Exception:
                continue
            for it in items:
                if it.get("status", "queued") not in ("queued", "done"):
                    continue
                dt = _parse_dt(it.get("slot"))
                if dt and dt >= now - timedelta(hours=1):
                    taken.append(dt)
    return taken


def _clear_of(cand, taken, min_sep: timedelta) -> bool:
    return all(abs((cand - t).total_seconds()) >= min_sep.total_seconds() for t in taken)


def next_free_gap(now, taken, horizon_h=24, min_lead_min=60, min_sep_min=45):
    """Next unoccupied slot: an official hole first, else the next 30-min tick."""
    lead = now + timedelta(minutes=min_lead_min)
    horizon = now + timedelta(hours=horizon_h)
    sep = timedelta(minutes=min_sep_min)
    # Earliest clear time wins: official holes and 30-min ticks together, so a
    # same-day interstitial gap is preferred over tomorrow's official hole.
    cands = []
    day0 = now.date()
    for d in range(0, 3):
        day = (day0 + timedelta(days=d)).isoformat()
        for hhmm in slot_times.official_times():
            dt = _parse_dt(slot_times.slot_iso(day, hhmm))
            if dt and lead <= dt <= horizon:
                cands.append(dt)
    t = lead.replace(second=0, microsecond=0)
    extra = (30 - (t.minute % 30)) % 30
    if extra:
        t += timedelta(minutes=extra)
    if t < lead:
        t += timedelta(minutes=30)
    while t <= horizon:
        cands.append(t)
        t += timedelta(minutes=30)
    for cand in sorted(set(cands)):
        if _clear_of(cand, taken, sep):
            return cand
    return None


def retry_error_post(r, x, now, late_h, all_r, posts):
    """Retry a YouTube-rejected Buffer post once. Returns (action, due, share_now, err).

    action: 'retry', 'dead', or 'skip'. Does not call the API.
    """
    err = ((x or {}).get("error") or {}).get("message") or r.get("buffer_error") or "Buffer error"
    if is_publish_dead(r):
        return "skip", None, False, err
    retries = int(r.get("buffer_retry_count") or 0)
    if retries >= 1:
        return "dead", None, False, err
    slot = _parse_dt(r.get("slot"))
    if slot is None:
        return "dead", None, False, err
    if slot >= now - timedelta(hours=late_h):
        share_now = slot <= now + timedelta(minutes=3)
        return "retry", slot, share_now, err
    taken = occupied_times(all_r, posts, now)
    # this result's original slot is failed; do not treat it as occupied
    taken = [t for t in taken if slot is None or abs((t - slot).total_seconds()) > 60]
    gap = next_free_gap(now, taken)
    if not gap:
        return "dead", None, False, "original slot long past; no free gap to retry: " + err
    return "retry", gap, False, err


LIMIT_MARKERS = ("daily posting limit", "posts per 24 hours")


def is_limit_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in LIMIT_MARKERS)


def _post_when(x):
    return _parse_dt(x.get("sentAt") or "") or _parse_dt(x.get("dueAt") or "")


def limit_points(posts):
    """Times that count toward Buffer's YouTube daily cap: sent + still-scheduled posts."""
    pts = []
    for x in posts or []:
        if (x.get("status") or "") not in ("sent", "scheduled"):
            continue
        dt = _post_when(x)
        if dt:
            pts.append(dt)
    return sorted(pts)


def _window_ok(points, cand, limit, window):
    """True if adding `cand` keeps every rolling window that contains it at <= limit.

    Only windows ending at cand or at a later point within `window` contain cand, so
    old busy days (before the cap existed) never block a new post.
    """
    pts = sorted(list(points) + [cand])
    for p in pts:
        if not (cand <= p < cand + window):
            continue
        if sum(1 for q in pts if p - window < q <= p) > limit:
            return False
    return True


def first_allowed_time(t, points, limit, window_h=24.0, margin_min=6.0):
    """Earliest time >= t a new post fits Buffer's rolling 24h YouTube cap.

    Buffer (Nov 19 2026) caps YouTube Shorts per channel per rolling 24h (Free plan: 2).
    Posts over the cap fail with 'daily posting limit'. Returns None if nothing in 48h.
    """
    if not limit or limit <= 0:
        return t
    window = timedelta(hours=window_h)
    margin = timedelta(minutes=margin_min)
    cands = sorted({t} | {p + window + margin for p in points if p + window + margin > t})
    for c in cands:
        if c > t + timedelta(hours=48):
            break
        if _window_ok(points, c, limit, window):
            return c
    return None


def posted_index(all_r, posts):
    """(hook_key, when, id, sent?) for everything already sent/scheduled (results + Buffer)."""
    idx = []
    for r in all_r:
        if r.get("publish_blocked") or backup_stock.must_not_publish(r):
            continue
        st = r.get("buffer_status")
        sent = bool(st == "sent" or r.get("youtube_url") or r.get("buffer_posted_at"))
        if not (sent or st == "scheduled"):
            continue
        k = backup_stock.hook_key(r.get("title") or r.get("hook") or "")
        when = _parse_dt(r.get("buffer_posted_at") or r.get("buffer_due_at") or r.get("slot"))
        idx.append({"key": k, "when": when, "id": r.get("id"), "post": r.get("buffer_post_id"),
                    "sent": sent, "source": str(r.get("source") or "")})
    for x in posts or []:
        st = x.get("status") or ""
        if st not in ("sent", "scheduled"):
            continue
        k = backup_stock.hook_key((x.get("metadata") or {}).get("title") or "")
        idx.append({"key": k, "when": _post_when(x), "id": None, "post": x.get("id"),
                    "sent": st == "sent", "source": ""})
    return idx


def repeat_of(r, idx, when=None, hist=None):
    """Reason string if r would re-post a hook or any source scene / video file that already went out.

    All time, not a 72h window: 20261117 and 20261124 both posted dandadanevil_scene1.
    `hist` is posted_history.index() (durable history file + posted results)."""
    if hist is not None:
        why = posted_history.conflict(r, hist)
        if why:
            return why
    me, mine = r.get("id"), r.get("buffer_post_id")
    k = backup_stock.hook_key(r.get("title") or r.get("hook") or "")
    src = str(r.get("source") or "")
    when = when or _parse_dt(r.get("slot"))
    for e in idx:
        if (e["id"] and e["id"] == me) or (mine and e["post"] == mine):
            continue
        earlier = e["sent"] or (e["when"] and when and e["when"] < when)
        if not earlier:
            continue
        if k and e["key"] == k:
            return f"same hook already posted ({e['id'] or e['post']}): {k!r}"
        if src and posted_history.clip_name(e["source"]) == posted_history.clip_name(src):
            return f"same source scene already posted ({e['id']}): {posted_history.clip_name(src)}"
    return None


def block_repeat(p, r, why, now, changed, cur=None):
    """Mark a repeat so it is never posted; delete its queued (never sent) Buffer post."""
    pid = r.get("buffer_post_id")
    live = cur or {}
    if pid and live.get("id") == pid and live.get("status") != "sent" and not DRY:
        try:
            delete_post(pid)
            r["buffer_deleted_post_id"] = pid
            print(f"{r.get('id')}: deleted queued repeat Buffer post {pid}")
        except Exception as e:
            print(f"{r.get('id')}: could not delete queued Buffer post {pid}: {e}")
    print(f"{r.get('id')}: NOT posting - repeat: {why}")
    if DRY:
        return
    r["publish_blocked"] = ("repeat: " + why)[:300]
    r["publish_blocked_at"] = now.strftime("%Y-%m-%d %H:%M IST")
    for k in ("buffer_post_id", "buffer_status", "buffer_due_at"):
        r.pop(k, None)
    save(p, r); changed.append(r.get("id"))


def metadata_errors(r):
    """Gate re-check on metadata (render-time gate already checked the video itself)."""
    g = r.get("gate") or {}
    body = (r.get("description_final") or r.get("description") or "").split("\n\nMusic:")[0]
    bad = sky_ai.banned_hits(r.get("title"), body)
    errs = hook_text.check_item_hook_fields(r) + (["banned: " + ",".join(bad)] if bad else [])
    if hook_text.template_errors(r.get("hook") or "") or hook_text.template_errors(r.get("title") or ""):
        errs.append("old 'The reason why' template; needs re-render")
    if g and g.get("ok") is False:
        errs.append("render gate failed")
    if not (r.get("title") or "").strip():
        errs.append("empty title")
    return errs


def main():
    now = datetime.now(IST)
    start = datetime.fromisoformat(CFG["post_from_slot"])
    late_h = float(CFG.get("late_post_hours") or 6)
    limit_defer_h = float(CFG.get("limit_defer_hours") or 12)
    daily_limit = int(CFG.get("buffer_daily_limit") or 0)
    posts = None
    idx = None
    hist = {}
    failed, changed = [], []
    rows = []
    for p in sorted(glob.glob(str(REPO / "results" / "*.json"))):
        try:
            rows.append((p, json.loads(Path(p).read_text())))
        except Exception:
            continue
    all_r = [r for _, r in rows]
    for p, r in rows:
        iid = r.get("id")
        if ONLY and iid not in ONLY:
            continue
        if r.get("status") != "done" or not r.get("asset_url") or not r.get("slot"):
            continue
        # Never post demo / dry-run / no_publish renders (test-renders release).
        # Unassigned stock has no slot and is skipped by the check above.
        if backup_stock.must_not_publish(r):
            continue
        if (r.get("id") or "").startswith("stock-") and not r.get("assigned_from_stock"):
            continue
        if is_publish_dead(r):
            continue
        if backup_stock.slot_post_conflicts(r, all_r):
            print(f"{iid}: slot already covered by another result; not posting")
            continue
        slot = datetime.fromisoformat(r["slot"])
        if slot < start:
            continue
        if posts is None:
            posts = channel_posts()
        if idx is None:
            idx = posted_index(all_r, posts)
            hist = posted_history.index(REPO, all_r)
        byid = {x["id"]: x for x in posts}
        # Repeat guard: never schedule a hook (or source clip) that already went out.
        cur = byid.get(r.get("buffer_post_id") or "") or {}
        if (cur.get("status") or r.get("buffer_status")) != "sent":
            why = repeat_of(r, idx, hist=hist)
            if why:
                block_repeat(p, r, why, now, changed, cur)
                idx[:] = [e for e in idx if e["id"] != iid and not (cur and e["post"] == cur.get("id"))]
                continue
        # Buffer's daily cap rejected the post: not a content error. Re-queue it (no retry
        # counted) inside limit_defer_hours of the slot, at the first time the cap allows.
        if r.get("buffer_post_id") and (cur.get("status") or r.get("buffer_status")) == "error" \
                and is_limit_error(((cur.get("error") or {}).get("message")) or r.get("buffer_error")):
            print(f"{iid}: Buffer post {r['buffer_post_id']} hit the daily posting limit; re-queueing")
            r["buffer_limit_failed_post_ids"] = list(r.get("buffer_limit_failed_post_ids") or []) + [r["buffer_post_id"]]
            for k in ("buffer_post_id", "buffer_status", "buffer_due_at", "buffer_error", "buffer_retry_count"):
                r.pop(k, None)
            r["limit_deferred"] = True
            if not DRY:
                save(p, r); changed.append(iid)
        # refresh status of an already-created post; a re-render with new hook/title edits the scheduled post
        if r.get("buffer_post_id"):
            x = byid.get(r["buffer_post_id"])
            bt = norm(((x or {}).get("metadata") or {}).get("title"))
            if x and x.get("status") == "scheduled" and bt and bt != norm(r["title"]) and r.get("asset_url"):
                print(f"{iid}: re-rendered with new title; editing scheduled Buffer post {x['id']} (same slot)")
                if not DRY:
                    try:
                        ed = edit(r, x["id"], x.get("dueAt"))
                        r.update(buffer_status=ed.get("status"), buffer_due_at=ed.get("dueAt"),
                                 buffer_edited_at=now.strftime("%Y-%m-%d %H:%M IST"))
                        save(p, r); changed.append(iid)
                        x["metadata"] = {"title": r["title"]}
                    except Exception as e:
                        failed.append(f"{iid}: edit failed: {e}")
                continue
            st = (x or {}).get("status") or r.get("buffer_status")
            if st == "error" or (x and x.get("error")):
                action, due, share_now, err = retry_error_post(r, x, now, late_h, all_r, posts)
                if x and x.get("error"):
                    r["buffer_error"] = x["error"].get("message") or err
                elif err:
                    r["buffer_error"] = err
                if action == "skip":
                    continue
                if action == "dead":
                    mark_publish_dead(r, "Buffer error after retry: " + (r.get("buffer_error") or err), now)
                    print(f"{iid}: Buffer post {r.get('buffer_post_id')} still error after retry; marking dead")
                    save(p, r); changed.append(iid)
                    continue
                # retry once: create a new post (leave the failed Buffer post alone; never delete YT videos)
                r["buffer_retry_count"] = int(r.get("buffer_retry_count") or 0) + 1
                r["buffer_prev_post_id"] = r.get("buffer_post_id")
                if due and _parse_dt(r.get("slot")) and due != _parse_dt(r.get("slot")):
                    r["slot_original"] = r.get("slot_original") or r.get("slot")
                    r["slot"] = due.isoformat()
                when = "shareNow" if share_now else f"schedule {due.isoformat()}"
                print(f"{iid}: Buffer post {r.get('buffer_prev_post_id')} error; retrying once ({when}): {r.get('buffer_error')}")
                if DRY:
                    save(p, r); changed.append(iid)
                    continue
                try:
                    post = create(r, due, share_now)
                except Exception as e:
                    mark_publish_dead(r, "retry create failed: " + str(e), now)
                    print(f"{iid}: retry create failed; marking dead: {e}")
                    save(p, r); changed.append(iid)
                    continue
                r.update(buffer_post_id=post["id"], buffer_status=post.get("status"),
                         buffer_due_at=post.get("dueAt"),
                         buffer_created_at=now.strftime("%Y-%m-%d %H:%M IST"))
                r.pop("buffer_error", None)
                save(p, r); changed.append(iid)
                posts.append({"id": post["id"], "status": post.get("status"),
                              "dueAt": post.get("dueAt"), "metadata": {"title": r["title"]}})
                print(f"{iid}: retry Buffer post {post['id']} status={post.get('status')} dueAt={post.get('dueAt')}")
                continue
            if x:
                upd = {"buffer_status": st, "youtube_url": x.get("externalLink") or r.get("youtube_url")}
                if x.get("sentAt"):
                    upd["buffer_posted_at"] = x["sentAt"]
                if any(r.get(k) != v for k, v in upd.items()):
                    r.update(upd); save(p, r); changed.append(iid)
            continue
        errs = metadata_errors(r)
        if errs:
            print(f"{iid}: NOT posting ({'; '.join(errs)})")
            continue
        # idempotency: same title already on the channel (Buffer or manual Studio upload)
        dup = next((x for x in posts if (x.get("status") or "") in ("sent", "scheduled")
                    and norm((x.get("metadata") or {}).get("title")) == norm(r["title"])), None)
        if dup:
            print(f"{iid}: already on Buffer as {dup['id']} ({dup.get('status')}); recording, not posting")
            r.update(buffer_post_id=dup["id"], buffer_status=dup.get("status"), buffer_due_at=dup.get("dueAt"),
                     youtube_url=dup.get("externalLink"), buffer_note="matched existing post by title")
            save(p, r); changed.append(iid)
            continue
        win_h = limit_defer_h if r.get("limit_deferred") else late_h
        if slot < now - timedelta(hours=win_h) and not (FORCE and ONLY and iid in ONLY):
            print(f"{iid}: slot {r['slot']} is more than {win_h}h past and not posted - needs a manual decision")
            failed.append(f"{iid}: missed slot not auto-posted (>{win_h}h late)")
            continue
        due = slot
        if daily_limit:
            want = max(slot, now + timedelta(minutes=2))
            ok_at = first_allowed_time(want, limit_points(posts), daily_limit)
            if ok_at is None or ok_at > slot + timedelta(hours=win_h):
                print(f"::warning::{iid}: Buffer YouTube cap ({daily_limit}/24h) is full until after "
                      f"{win_h}h past the slot; not posting (video kept)")
                continue
            if ok_at > slot + timedelta(minutes=3):
                due = ok_at
                print(f"{iid}: Buffer cap {daily_limit}/24h -> scheduling at {due:%Y-%m-%d %H:%M IST} instead of {slot:%H:%M}")
        if due != slot:
            share_now = False
            slot = due
        else:
            share_now = slot <= now + timedelta(minutes=3)
        print(f"{iid}: {'shareNow' if share_now else 'schedule ' + r['slot']} -> Buffer channel {CH}: {r['title']!r}")
        if DRY:
            continue
        try:
            post = create(r, slot, share_now)
        except Exception as e:
            failed.append(f"{iid}: {e}")
            r.update(buffer_status="create_failed", buffer_error=str(e)[:300]); save(p, r); changed.append(iid)
            continue
        r.update(buffer_post_id=post["id"], buffer_status=post.get("status"), buffer_due_at=post.get("dueAt"),
                 buffer_created_at=now.strftime("%Y-%m-%d %H:%M IST"))
        save(p, r); changed.append(iid)
        posts.append({"id": post["id"], "status": post.get("status"), "dueAt": post.get("dueAt"),
                      "metadata": {"title": r["title"]}})
        idx.append({"key": backup_stock.hook_key(r["title"]), "when": slot, "id": iid, "post": post["id"],
                    "sent": False, "source": str(r.get("source") or "")})
        for k in posted_history.keys_of(r):
            hist.setdefault(k, iid)
        try:
            posted_history.record(r, REPO, now=now)
        except Exception as e:
            print(f"{iid}: posted history not written: {e}")
        print(f"{iid}: Buffer post {post['id']} status={post.get('status')} dueAt={post.get('dueAt')}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        open(out, "a").write(f"changed={','.join(changed)}\n")
    if failed:
        print("::error::" + " | ".join(failed)[:900])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
