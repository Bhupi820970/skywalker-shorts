#!/usr/bin/env python3
"""Backup topic fallback + 18s publish gate (light, no OpenCV)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gate
import plan

IST = timezone(timedelta(hours=5, minutes=30))


def _fill_other_official(items, keep_slot, now, ahead=6, past_h=6):
    """Keep other official times 'still rendering' so only keep_slot is tested."""
    import slot_times
    day0 = now.date()
    for d in range(0, 2):
        day = (day0 + timedelta(days=d)).isoformat()
        for hhmm in slot_times.official_times():
            iso = slot_times.slot_iso(day, hhmm)
            try:
                dt = datetime.fromisoformat(iso)
            except Exception:
                continue
            if not (now - timedelta(hours=past_h) <= dt <= now + timedelta(hours=ahead)):
                continue
            if iso == keep_slot or any(it.get("slot") == iso for it in items.values()):
                continue
            iid = f"keep-{day}-{hhmm.replace(':', '')}"
            items[iid] = {"id": iid, "slot": iso, "status": "queued"}
    return items


def test_gate_min_duration_is_18():
    assert gate.MIN_DUR == 18.0
    assert gate.MAX_DUR == 32.6


def test_failed_backup_picks_next_topic(tmp_path, monkeypatch):
    now = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
    slot = "2026-11-13T12:30:00+05:30"
    items = {"war2": {"id": "war2", "slot": slot, "status": "queued"}}
    _fill_other_official(items, slot, now)
    results = {"war2": {"status": "failed"}}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "20261113-1230-zootopia2.json").write_text(
        json.dumps({"status": "failed", "backup_item": "backup-zootopia2"})
    )
    pool = [
        {"id": "backup-zootopia2", "fallback_asset": "zootopia2_scene1_h264.mp4"},
        {"id": "backup-kalki2898ad", "fallback_asset": "kalki2898ad_scene1_h264.mp4"},
        {"id": "backup-superman", "fallback_asset": "superman_scene1_h264.mp4"},
    ]
    picks, used = plan.select_slot_backups(
        items, lambda i: results.get(i), pool, set(), now, ahead=6, busy=set()
    )
    assert picks == ["backup-kalki2898ad@20261113-1230"], picks
    assert "backup-zootopia2" in used


def test_done_backup_does_not_pick_another(tmp_path, monkeypatch):
    now = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
    slot = "2026-11-13T12:30:00+05:30"
    items = {"war2": {"id": "war2", "slot": slot, "status": "queued"}}
    _fill_other_official(items, slot, now)
    results = {"war2": {"status": "failed"}}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "20261113-1230-zootopia2.json").write_text(
        json.dumps({"status": "done", "backup_item": "backup-zootopia2"})
    )
    pool = [{"id": "backup-zootopia2", "fallback_asset": "zootopia2_scene1_h264.mp4"}, {"id": "backup-kalki2898ad", "fallback_asset": "kalki2898ad_scene1_h264.mp4"}]
    picks, used = plan.select_slot_backups(
        items, lambda i: results.get(i), pool, set(), now, ahead=6, busy=set()
    )
    assert picks == []


def test_no_backup_until_all_live_items_failed(tmp_path, monkeypatch):
    now = datetime(2026, 11, 13, 10, 0, tzinfo=IST)
    slot = "2026-11-13T12:30:00+05:30"
    items = {"war2": {"id": "war2", "slot": slot, "status": "queued"}}
    _fill_other_official(items, slot, now)
    results = {"war2": {"status": "retry_queued"}}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    (tmp_path / "results").mkdir()
    pool = [{"id": "backup-zootopia2"}]
    picks, _ = plan.select_slot_backups(
        items, lambda i: results.get(i), pool, set(), now, ahead=6, busy=set()
    )
    assert picks == []
