"""Official YouTube/X link intake: validation, trailer block, next free slot."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import submit_link

IST = timezone(timedelta(hours=5, minutes=30))


def test_supported_and_trailer_urls():
    assert submit_link.is_supported_link("https://www.youtube.com/watch?v=abcdefghijk")
    assert submit_link.is_supported_link("https://youtu.be/abcdefghijk")
    assert submit_link.is_supported_link("https://x.com/Studio/status/123")
    assert not submit_link.is_supported_link("https://www.dailymotion.com/video/x123")
    assert submit_link.link_is_trailer("https://www.youtube.com/watch?v=abc", "Official Trailer")
    assert submit_link.link_is_trailer("https://youtu.be/abc?title=teaser")
    assert not submit_link.link_is_trailer("https://www.youtube.com/watch?v=abc", "Official fight scene")


def test_build_item_and_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    (tmp_path / "queue").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "links").mkdir()
    (tmp_path / "config").mkdir()
    now = datetime(2026, 11, 17, 16, 0, tzinfo=IST)
    # occupy 16:00 and 18:30 so the next free official slot is 20:30
    for hhmm in ("16:00", "18:30"):
        iso = f"2026-11-17T{hhmm}:00+05:30"
        (tmp_path / "results" / f"20261117-{hhmm.replace(':','')}-taken.json").write_text(json.dumps({
            "id": f"20261117-{hhmm.replace(':','')}-taken", "status": "done", "slot": iso,
        }))
    iso = submit_link.next_free_slot(tmp_path, now=now, min_ahead_hours=3)
    assert iso == "2026-11-17T20:30:00+05:30"
    item = submit_link.build_item(
        "https://www.youtube.com/watch?v=abcdefghijk",
        title="Vinland Saga",
        hook="Why Thorfinn dropped his daggers forever",
        name="vinlandsagafight",
        slot_iso=iso,
        words="VINLAND SAGA",
    )
    assert item["id"] == "20261117-2030-vinlandsagafight"
    assert item["fallback_asset"] == "vinlandsagafight_scene1_h264.mp4"
    assert "trailer" not in item["fallback_asset"]
    assert item["content_kind"] == "best scene"
    path = submit_link.append_queue_item(item, tmp_path)
    saved = json.loads(path.read_text())
    assert saved[0]["id"] == item["id"]


def test_build_item_rejects_trailer():
    try:
        submit_link.build_item(
            "https://www.youtube.com/watch?v=abcdefghijk",
            title="Wicked Official Trailer",
            name="wickedforgood",
            slot_iso="2026-11-18T09:00:00+05:30",
        )
    except SystemExit as e:
        assert "trailer" in str(e).lower()
    else:
        raise AssertionError("trailer title must be rejected")
