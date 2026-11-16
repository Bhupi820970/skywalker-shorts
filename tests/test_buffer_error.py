"""Buffer YouTube-reject errors: retry once, then mark dead (no network)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import buffer_post  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def test_next_free_gap_skips_occupied():
    now = datetime(2026, 11, 16, 8, 6, tzinfo=IST)
    taken = [
        datetime(2026, 11, 16, 6, 30, tzinfo=IST),
        datetime(2026, 11, 16, 9, 0, tzinfo=IST),
        datetime(2026, 11, 16, 11, 30, tzinfo=IST),
        datetime(2026, 11, 16, 12, 30, tzinfo=IST),
        datetime(2026, 11, 16, 13, 30, tzinfo=IST),
        datetime(2026, 11, 16, 16, 0, tzinfo=IST),
        datetime(2026, 11, 16, 17, 30, tzinfo=IST),
        datetime(2026, 11, 16, 18, 30, tzinfo=IST),
        datetime(2026, 11, 16, 20, 30, tzinfo=IST),
        datetime(2026, 11, 16, 22, 30, tzinfo=IST),
    ]
    gap = buffer_post.next_free_gap(now, taken)
    assert gap is not None
    # 10:00 sits between 09:00 and 11:30, at least 45 min clear, 60+ min from now
    assert gap == datetime(2026, 11, 16, 10, 0, tzinfo=IST)


def test_late_error_retries_into_gap():
    now = datetime(2026, 11, 16, 8, 6, tzinfo=IST)
    r = {
        "id": "20261115-1600-hoppers",
        "slot": "2026-11-15T16:00:00+05:30",
        "buffer_post_id": "6af968fe918fe79d2ece14d7",
        "buffer_status": "error",
        "buffer_error": "unverified YouTube account",
        "title": "She put her whole mind into a beaver in Pixar Hoppers #hoppers #pixar #animation",
    }
    x = {"id": r["buffer_post_id"], "status": "error",
         "error": {"message": "unverified YouTube account"}}
    others = [
        {"id": "20261116-0900-tronares", "slot": "2026-11-16T09:00:00+05:30",
         "status": "done", "asset_url": "https://x/a.mp4",
         "buffer_status": "scheduled", "buffer_due_at": "2026-11-16T03:30:00.000Z",
         "title": "Ares aura in Tron Ares is unreal #tronares #jaredleto #scifi"},
        {"id": "20261116-1330-superman", "slot": "2026-11-16T13:30:00+05:30",
         "status": "done", "asset_url": "https://x/b.mp4",
         "buffer_status": "scheduled", "buffer_due_at": "2026-11-16T08:00:00.000Z"},
        {"id": "20261116-1830-nobody2", "slot": "2026-11-16T18:30:00+05:30",
         "status": "done", "asset_url": "https://x/c.mp4",
         "buffer_status": "scheduled", "buffer_due_at": "2026-11-16T13:00:00.000Z"},
        {"id": "20261116-0630-predatorbadlands", "slot": "2026-11-16T06:30:00+05:30",
         "status": "done", "asset_url": "https://x/d.mp4",
         "buffer_status": "scheduled", "buffer_due_at": "2026-11-16T01:00:00.000Z"},
        {"id": "20261116-1230-housefull5", "slot": "2026-11-16T12:30:00+05:30",
         "status": "done", "asset_url": "https://x/e.mp4"},
    ]
    posts = [{"id": o["buffer_post_id"], "status": "scheduled",
              "dueAt": o["buffer_due_at"], "metadata": {"title": o.get("title")}}
             for o in others if o.get("buffer_post_id") or o.get("buffer_due_at")
             for o in [dict(o, buffer_post_id=o.get("buffer_post_id") or "p")]
             if o.get("buffer_status") == "scheduled"]
    posts.append(x)
    action, due, share_now, err = buffer_post.retry_error_post(r, x, now, 6, others + [r], posts)
    assert action == "retry"
    assert share_now is False
    assert due == datetime(2026, 11, 16, 10, 0, tzinfo=IST)
    assert "unverified" in err


def test_error_after_retry_is_dead_not_failed():
    now = datetime(2026, 11, 16, 8, 6, tzinfo=IST)
    r = {
        "id": "20261115-1600-hoppers",
        "slot": "2026-11-16T10:00:00+05:30",
        "buffer_post_id": "new-retry-id",
        "buffer_retry_count": 1,
        "buffer_status": "error",
        "buffer_error": "still rejected",
    }
    x = {"id": "new-retry-id", "status": "error", "error": {"message": "still rejected"}}
    action, due, share_now, err = buffer_post.retry_error_post(r, x, now, 6, [r], [x])
    assert action == "dead"
    assert due is None
    buffer_post.mark_publish_dead(r, "Buffer error after retry: " + err, now)
    assert r["buffer_status"] == "dead"
    assert r["publish_dead"] is True
    assert r.get("status") != "dead"  # render stays done
    assert buffer_post.is_publish_dead(r)


def test_same_slot_retry_when_still_due():
    now = datetime(2026, 11, 15, 16, 10, tzinfo=IST)
    r = {"id": "h", "slot": "2026-11-15T16:00:00+05:30", "buffer_status": "error"}
    x = {"id": "bp", "status": "error", "error": {"message": "unverified"}}
    action, due, share_now, err = buffer_post.retry_error_post(r, x, now, 6, [r], [x])
    assert action == "retry"
    assert share_now is True  # slot already passed but within late_h
    assert due.hour == 16


def test_dead_skip_and_scheduled_untouched():
    assert buffer_post.is_publish_dead({"buffer_status": "dead"})
    assert not buffer_post.is_publish_dead({"buffer_status": "scheduled"})
    now = datetime(2026, 11, 16, 8, 6, tzinfo=IST)
    r = {"id": "t", "slot": "2026-11-16T09:00:00+05:30", "buffer_status": "scheduled",
         "publish_dead": False}
    # scheduled is not an error retry
    assert not buffer_post.is_publish_dead(r)
