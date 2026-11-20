"""Health coverage: a slot is covered only by a finished publishable render or ready stock mp4."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backup_stock
import health_check
from test_backup_stock import _ready_result, _root, _write

IST = timezone(timedelta(hours=5, minutes=30))
SCENE = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/{slug}_scene1_h264.mp4"


def _scene(slug: str) -> str:
    return SCENE.format(slug=slug)


def _queue_item(day: str, hhmm: str, slug: str, status: str = "queued") -> dict:
    iid = f"{day.replace('-', '')}-{hhmm.replace(':', '')}-{slug}"
    iso = f"{day}T{hhmm}:00+05:30"
    return {
        "id": iid,
        "slot": iso,
        "status": status,
        "fallback_asset": f"{slug}_scene1_h264.mp4",
        "fallback_mp4": [_scene(slug)],
        "sources": [_scene(slug)],
    }


def _no_audit(monkeypatch):
    monkeypatch.setattr(backup_stock, "audit_lost_assets", lambda *a, **k: [])
    monkeypatch.setattr(backup_stock, "release_asset_names", lambda *a, **k: set())


def test_queued_fallback_is_not_coverage(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    _no_audit(monkeypatch)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
    rows = [
        _queue_item("2026-11-20", hhmm, slug)
        for hhmm, slug in (
            ("06:30", "apothecarydiaries2"),
            ("09:00", "firforce3"),
            ("11:30", "aotfinal"),
            ("13:30", "onepunchman3"),
            ("16:00", "suzume"),
            ("18:30", "cyberpunkedgerunners"),
            ("20:30", "dandadan2"),
            ("22:30", "weatheringwithyou"),
        )
    ]
    _write(root / "queue" / "2026-11-20.json", rows)
    status = health_check.check(root, now)
    # 06:30 is >2h past, so it is out of the health window.
    short_hhmm = {s[11:16] for s in status["short"]}
    assert "11:30" in short_hhmm
    assert "13:30" in short_hhmm
    assert "09:00" in short_hhmm
    assert not any(r.get("how") == "queued+fallback" for r in status["ready"])
    assert "20261120-1130-aotfinal" in status["need_render"]
    assert status["ok"] is False


def test_finished_publishable_render_covers_slot(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    _no_audit(monkeypatch)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
    item = _queue_item("2026-11-20", "11:30", "aotfinal")
    _write(root / "queue" / "2026-11-20.json", [item])
    _write(root / "results" / f"{item['id']}.json",
           _ready_result(item["id"], slot=item["slot"]))
    status = health_check.check(root, now)
    covered = [r for r in status["ready"] if r["slot"] == item["slot"]]
    assert covered and covered[0]["how"] == "done"
    assert item["slot"] not in status["short"]
    assert item["id"] not in status["need_render"]


def test_ready_stock_mp4_covers_missing_slot(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch, pool=[
        {"id": "backup-superman", "fallback_asset": "superman_scene1_h264.mp4"},
    ])
    _no_audit(monkeypatch)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
    # Cover every other official time in the assign/health window so the
    # single ready stock video is assigned to 11:30, not an earlier hole.
    rows = []
    for hhmm, slug in (
        ("06:30", "apothecarydiaries2"),
        ("09:00", "firforce3"),
        ("13:30", "onepunchman3"),
        ("16:00", "suzume"),
        ("18:30", "cyberpunkedgerunners"),
        ("20:30", "dandadan2"),
        ("22:30", "weatheringwithyou"),
    ):
        item = _queue_item("2026-11-20", hhmm, slug)
        rows.append(item)
        _write(root / "results" / f"{item['id']}.json",
               _ready_result(item["id"], slot=item["slot"]))
    # Tomorrow's official times in the 24h horizon also need a done result
    # or they will consume stock first.
    for hhmm, slug in (("06:30", "haikyuudumpster"), ("09:00", "dandadan")):
        item = _queue_item("2026-11-21", hhmm, slug)
        rows.append(item)
        _write(root / "results" / f"{item['id']}.json",
               _ready_result(item["id"], slot=item["slot"]))
    _write(root / "queue" / "2026-11-20.json", rows)
    iid = "stock-superman-01"
    _write(root / "results" / f"{iid}.json",
           _ready_result(iid, stock_topic="backup-superman"))
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": [
            {"id": iid, "slug": "superman", "topic_id": "backup-superman",
             "status": "ready", "asset_url": _ready_result(iid)["asset_url"]},
        ]
    })
    slot = "2026-11-20T11:30:00+05:30"
    status = health_check.check(root, now)
    covered = [r for r in status["ready"] if r["slot"] == slot]
    assert covered, status
    assert covered[0]["how"] == "backup_stock"
    assert slot not in status["short"]


def test_unrendered_backup_pool_is_not_coverage(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch, pool=[
        {"id": "backup-suzume", "fallback_asset": "suzume_scene1_h264.mp4"},
    ])
    _no_audit(monkeypatch)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
    item = _queue_item("2026-11-20", "16:00", "suzume")
    _write(root / "queue" / "2026-11-20.json", [item])
    _write(root / "results" / f"{item['id']}.json",
           {"id": item["id"], "slot": item["slot"], "status": "failed"})
    status = health_check.check(root, now)
    assert item["slot"] in status["short"]
    assert not any(r.get("how") == "backup_pool" for r in status["ready"])
