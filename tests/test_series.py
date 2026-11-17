"""Part 2/3 sequels for Shorts that beat the channel median views."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import series
import weekly_hits

IST = timezone(timedelta(hours=5, minutes=30))


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def _root(tmp_path: Path):
    (tmp_path / "stats").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "queue").mkdir()
    (tmp_path / "config").mkdir()
    _write(tmp_path / "config" / "topic_catalog.json", {
        "items": [{
            "slug": "suzume",
            "pillar": "romance",
            "words": "SUZUME",
            "hook": "She keeps closing doors alone",
            "title": "She keeps closing doors alone | Suzume | #edit",
            "description": "Official scene.",
            "tags": ["suzume", "anime"],
            "scene_assets": ["suzume_scene1_h264.mp4", "suzume_scene2_h264.mp4", "suzume_scene3_h264.mp4"],
        }]
    })
    return tmp_path


def test_median_views():
    assert series.median_views([1, 3, 2]) == 2
    assert series.median_views([4, 2]) == 3
    assert series.median_views([]) == 0


def test_unused_clips_skip_used_and_trailers():
    topic = {"scene_assets": ["suzume_scene1_h264.mp4", "suzume_scene2_h264.mp4", "suzume_trailer_h264.mp4"]}
    used = series.used_shots({
        "source": "gh:https://github.com/o/r/releases/download/sources/suzume_scene1_h264.mp4",
        "fallback_mp4": [],
    })
    assert "suzume_scene1_h264.mp4" in used
    clips = series.unused_official_clips(topic, used)
    assert clips == ["suzume_scene2_h264.mp4"]


def test_sequel_hooks_differ_from_parent():
    hooks = series.sequel_hooks(
        {"hook": "She keeps closing doors alone", "words": "SUZUME"},
        used_hooks={"She keeps closing doors alone"},
    )
    assert hooks
    assert "She keeps closing doors alone" not in hooks
    assert all("reason why" not in h.lower() for h in hooks)


def test_queue_sequels_for_hit_title(tmp_path):
    root = _root(tmp_path)
    parent = {
        "id": "20261113-1730-suzume",
        "status": "done",
        "hook": "She keeps closing doors alone",
        "title": "She keeps closing doors alone | Suzume | #edit",
        "source": "gh:https://github.com/o/r/releases/download/sources/suzume_scene1_h264.mp4",
        "fallback_asset": "suzume_scene1_h264.mp4",
        "tags": ["suzume", "anime"],
    }
    _write(root / "results" / "20261113-1730-suzume.json", parent)
    _write(root / "stats" / "latest.json", {
        "videos": {
            "aaa": {"title": parent["title"], "views": 5000, "likes": 40,
                    "queue_id": "20261113-1730-suzume", "hook": parent["hook"]},
            "bbb": {"title": "A miss", "views": 10, "queue_id": "20261113-0900-other"},
        }
    })
    now = datetime(2026, 11, 17, 21, 0, tzinfo=IST)
    got = series.queue_sequels(root, now=now, max_titles=2)
    parts = {g["part"] for g in got}
    assert parts == {2, 3}
    assert all(g["slug"] == "suzume" for g in got)
    assert len({g["hook"] for g in got} | {parent["hook"]}) == 3
    assert len({g["asset"] for g in got} | {"suzume_scene1_h264.mp4"}) == 3
    assert "suzume_scene1_h264.mp4" not in {g["asset"] for g in got}
    # Idempotent: already queued parts are not duplicated.
    again = series.queue_sequels(root, now=now, max_titles=2)
    assert again == []


def test_below_median_does_not_queue(tmp_path):
    root = _root(tmp_path)
    _write(root / "results" / "20261113-1730-suzume.json", {
        "id": "20261113-1730-suzume", "status": "done", "hook": "Hi",
        "source": "gh:https://x/suzume_scene1_h264.mp4", "tags": ["suzume"],
    })
    _write(root / "stats" / "latest.json", {
        "videos": {
            "aaa": {"title": "t", "views": 5, "queue_id": "20261113-1730-suzume"},
            "bbb": {"title": "u", "views": 100, "queue_id": "other"},
        }
    })
    assert series.queue_sequels(root, now=datetime(2026, 11, 17, 21, 0, tzinfo=IST)) == []
