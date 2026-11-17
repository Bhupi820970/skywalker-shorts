"""Slot guardian: Buffer coverage, stock assign, evergreen last resort, no_publish."""
from __future__ import annotations

import json
import sys
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import backup_stock
import slot_guardian
from test_backup_stock import _ready_result, _root, _write

IST = timezone(timedelta(hours=5, minutes=30))


def test_buffer_covers_slot_by_due_time():
    slot = "2026-11-17T22:30:00+05:30"
    posts = [{"id": "p1", "status": "scheduled", "dueAt": "2026-11-17T17:00:00.000Z",
              "metadata": {"title": "A hook"}}]
    assert slot_guardian.buffer_covers_slot(posts, slot)
    assert not slot_guardian.buffer_covers_slot(posts, "2026-11-17T20:30:00+05:30")
    assert not slot_guardian.buffer_covers_slot([], slot)


def test_guardian_assigns_ready_stock_when_buffer_empty(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch, pool=[
        {"id": "backup-zootopia2", "fallback_asset": "zootopia2_scene1_h264.mp4"},
    ])
    iid = "stock-zootopia2-01"
    _write(root / "results" / f"{iid}.json", _ready_result(iid, title="Unique Zootopia hook"))
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": [
            {"id": iid, "slug": "zootopia2", "topic_id": "backup-zootopia2",
             "status": "ready", "asset_url": _ready_result(iid)["asset_url"]},
        ]
    })
    now = backup_stock.parse_slot("2026-11-17T22:10:00+05:30") if hasattr(backup_stock, "parse_slot") else None
    from datetime import datetime
    now = datetime.fromisoformat("2026-11-17T22:10:00+05:30")
    report = slot_guardian.guard(root, now, posts=[], fetch_buffer=False, lead_min=45, past_min=20)
    assert report["need_publish"]
    assert report["assigned"]
    assert not report["uncovered"]
    assigned = report["assigned"][0]
    r = json.loads((root / "results" / f"{assigned}.json").read_text())
    assert r["slot"].startswith("2026-11-17T22:30")
    assert r.get("assigned_from_stock")
    assert not backup_stock.must_not_publish(r)


def test_guardian_never_posts_or_assigns_demo(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    demo = _ready_result("demo-v5-romance", no_publish=True, demo=True, slot="2026-11-17T22:30:00+05:30",
                         asset_url="https://github.com/chatgpt820970/skywalker-shorts/releases/download/test-renders/test-demo-v5-romance.mp4")
    _write(root / "results" / "demo-v5-romance.json", demo)
    from datetime import datetime
    now = datetime.fromisoformat("2026-11-17T22:10:00+05:30")
    assert slot_guardian.publishable_for_slot(root, "2026-11-17T22:30:00+05:30") is None
    report = slot_guardian.guard(root, now, posts=[], fetch_buffer=False)
    assert "demo-v5-romance" not in (report.get("need_publish") or [])
    assert "demo-v5-romance" not in (report.get("assigned") or [])


def test_guardian_skips_already_posted_evergreen(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch, pool=[
        {"id": "backup-zootopia2", "fallback_asset": "zootopia2_scene1_h264.mp4"},
    ])
    posted = _ready_result("stock-zootopia2-01", title="Same Title Hook",
                           buffer_post_id="p1", buffer_status="sent",
                           stock_used=True, assigned_slot="2026-11-16T22:30:00+05:30")
    _write(root / "results" / "stock-zootopia2-01.json", posted)
    # A second unused ready video with the same title must not be picked.
    unused = _ready_result("stock-zootopia2-02", title="Same Title Hook")
    _write(root / "results" / "stock-zootopia2-02.json", unused)
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "items": [
            {"id": "stock-zootopia2-01", "slug": "zootopia2", "status": "used",
             "asset_url": posted["asset_url"], "assigned_slot": posted["assigned_slot"]},
            {"id": "stock-zootopia2-02", "slug": "zootopia2", "status": "ready",
             "asset_url": unused["asset_url"]},
        ]
    })
    from datetime import datetime
    now = datetime.fromisoformat("2026-11-17T22:10:00+05:30")
    posts = [{"id": "p1", "status": "sent", "dueAt": "2026-11-16T17:00:00.000Z",
              "metadata": {"title": "Same Title Hook"}}]
    ent = slot_guardian.pick_evergreen(root, "2026-11-17", now, posts)
    assert ent is None
