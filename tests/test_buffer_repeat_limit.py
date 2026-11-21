"""Publish never re-posts a hook, and respects Buffer's 2/24h YouTube cap (no network)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import backup_stock  # noqa: E402
import buffer_post  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
Z = timezone.utc


def test_hook_key_ignores_hashtags_and_show_suffix():
    a = "Why Hodaka cannot stop watching her #weatheringwithyou #hina #anime"
    b = "Why Hodaka cannot stop watching her #weatheringwithyou #hina #animeedit"
    assert backup_stock.hook_key(a) == backup_stock.hook_key(b) == "why hodaka cannot stop watching her"
    assert backup_stock.hook_key("David wanted to be a legend | Cyberpunk Edgerunners | #edit") == \
        "david wanted to be a legend"


def test_first_allowed_time_rolling_cap():
    sent = [datetime(2026, 11, 20, 13, 1, tzinfo=Z), datetime(2026, 11, 20, 17, 2, tzinfo=Z)]
    want = datetime(2026, 11, 21, 3, 30, tzinfo=Z)  # 09:00 IST
    got = buffer_post.first_allowed_time(want, sent, 2)
    assert got == datetime(2026, 11, 21, 13, 7, tzinfo=Z)  # 18:37 IST
    got2 = buffer_post.first_allowed_time(want, sent + [got], 2)
    assert got2 == datetime(2026, 11, 21, 17, 8, tzinfo=Z)  # 22:38 IST
    assert buffer_post.first_allowed_time(want, [], 2) == want
    # an old busy day (6 posts on 18 Nov, before the cap) must not block today
    busy = [datetime(2026, 11, 18, h, 0, tzinfo=Z) for h in range(8, 14)]
    assert buffer_post.first_allowed_time(want, busy + sent, 2) == got


def _res(iid, slot, title, **kw):
    r = {"id": iid, "status": "done", "asset_url": f"https://x/{iid}.mp4", "slot": slot, "title": title,
         "hook": title.split(" #")[0], "description": title, "qc_status": "pass"}
    r.update(kw)
    return r


def _run(monkeypatch, tmp_path, rows, posts, now):
    res = tmp_path / "results"
    res.mkdir()
    for r in rows:
        (res / f"{r['id']}.json").write_text(json.dumps(r))
    monkeypatch.setattr(buffer_post, "REPO", tmp_path)
    (tmp_path / "queue").mkdir()
    monkeypatch.setattr(buffer_post, "CFG", dict(buffer_post.CFG, buffer_daily_limit=2, limit_defer_hours=12,
                                               late_post_hours=6, post_from_slot="2026-11-01T00:00:00+05:30"))
    monkeypatch.setattr(buffer_post, "channel_posts", lambda: [dict(p) for p in posts])
    created, deleted = [], []

    def fake_create(r, due, share_now):
        created.append((r["id"], due, share_now))
        return {"id": "new-" + r["id"], "status": "scheduled", "dueAt": due.astimezone(Z).isoformat()}

    monkeypatch.setattr(buffer_post, "create", fake_create)
    monkeypatch.setattr(buffer_post, "delete_post", lambda pid: deleted.append(pid) or pid)
    monkeypatch.setattr(buffer_post.hook_text, "check_item_hook_fields", lambda r: [])
    monkeypatch.setattr(buffer_post.hook_text, "template_errors", lambda t: [])
    monkeypatch.setattr(buffer_post.sky_ai, "banned_hits", lambda *a: [])
    monkeypatch.setattr(buffer_post.backup_stock, "is_ready_result", lambda r: r.get("status") == "done")

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(buffer_post, "datetime", FakeDT)
    buffer_post.main()
    out = {p.stem: json.loads(p.read_text()) for p in res.glob("*.json")}
    return created, deleted, out


def test_repeat_queued_post_is_deleted_and_blocked(monkeypatch, tmp_path):
    now = datetime(2026, 11, 21, 12, 0, tzinfo=IST)
    old = _res("20261120-2230-weatheringwithyou", "2026-11-20T22:30:00+05:30",
               "Why Hodaka cannot stop watching her #weatheringwithyou #hina #anime",
               buffer_post_id="p1", buffer_status="sent", youtube_url="https://youtu.be/x")
    rep = _res("20261121-1330-weatheringwithyou", "2026-11-21T13:30:00+05:30",
               "Why Hodaka cannot stop watching her #weatheringwithyou #hina #animeedit",
               assigned_from_stock=True, buffer_post_id="p2", buffer_status="scheduled")
    posts = [
        {"id": "p1", "status": "sent", "sentAt": "2026-11-20T17:02:29Z",
         "metadata": {"title": old["title"]}},
        {"id": "p2", "status": "scheduled", "dueAt": "2026-11-21T08:00:00.000Z",
         "metadata": {"title": rep["title"]}},
    ]
    created, deleted, out = _run(monkeypatch, tmp_path, [old, rep], posts, now)
    assert deleted == ["p2"] and not created
    r = out["20261121-1330-weatheringwithyou"]
    assert r["publish_blocked"].startswith("repeat:") and "buffer_post_id" not in r


def test_new_repeat_never_created(monkeypatch, tmp_path):
    now = datetime(2026, 11, 21, 12, 0, tzinfo=IST)
    old = _res("a", "2026-11-20T22:30:00+05:30", "Why Hodaka cannot stop watching her #x",
               buffer_post_id="p1", buffer_status="sent")
    new = _res("20261121-1330-b", "2026-11-21T13:30:00+05:30", "Why Hodaka cannot stop watching her #y")
    posts = [{"id": "p1", "status": "sent", "sentAt": "2026-11-20T17:02:29Z", "metadata": {"title": old["title"]}}]
    created, _, out = _run(monkeypatch, tmp_path, [old, new], posts, now)
    assert not created and out["20261121-1330-b"]["publish_blocked"]


def test_limit_error_requeues_into_cap_window(monkeypatch, tmp_path):
    """09:00 Dandadan failed on Buffer's daily cap: re-post at the first allowed time today."""
    now = datetime(2026, 11, 21, 14, 40, tzinfo=IST)
    msg = "The daily posting limit for this channel has been reached! Please try reducing ..."
    dd = _res("20261121-0900-dandadan", "2026-11-21T09:00:00+05:30",
              "When the maid cafe order gets weird #dandadan #okarun #anime",
              buffer_post_id="e2", buffer_status="error", buffer_retry_count=1)
    posts = [
        {"id": "o%d" % h, "status": "sent", "sentAt": "2026-11-18T%02d:00:00Z" % h, "metadata": {"title": "O%d" % h}}
        for h in range(8, 14)
    ] + [
        {"id": "s1", "status": "sent", "sentAt": "2026-11-20T13:01:08Z", "metadata": {"title": "A"}},
        {"id": "s2", "status": "sent", "sentAt": "2026-11-20T17:02:29Z", "metadata": {"title": "B"}},
        {"id": "e2", "status": "error", "dueAt": "2026-11-21T07:30:47Z", "error": {"message": msg},
         "metadata": {"title": dd["title"]}},
    ]
    created, _, out = _run(monkeypatch, tmp_path, [dd], posts, now)
    assert len(created) == 1
    iid, due, share_now = created[0]
    assert not share_now and due.astimezone(Z) == datetime(2026, 11, 21, 13, 7, 8, tzinfo=Z)
    assert out["20261121-0900-dandadan"]["buffer_post_id"] == "new-20261121-0900-dandadan"
    assert "e2" in out["20261121-0900-dandadan"]["buffer_limit_failed_post_ids"]


def test_cap_full_skips_without_failing(monkeypatch, tmp_path):
    now = datetime(2026, 11, 21, 20, 0, tzinfo=IST)
    r = _res("20261121-2030-x", "2026-11-21T20:30:00+05:30", "Some new hook here #x")
    posts = [
        {"id": "s1", "status": "scheduled", "dueAt": "2026-11-21T13:07:00Z", "metadata": {"title": "A"}},
        {"id": "s2", "status": "scheduled", "dueAt": "2026-11-21T17:08:00Z", "metadata": {"title": "B"}},
    ]
    created, _, _ = _run(monkeypatch, tmp_path, [r], posts, now)
    assert not created
