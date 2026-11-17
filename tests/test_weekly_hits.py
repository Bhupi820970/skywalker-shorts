"""Weekly hit report and perf weight feed-through."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import perf
import weekly_hits

IST = timezone(timedelta(hours=5, minutes=30))


def test_weekly_hits_ranks_titles_hooks_songs(tmp_path: Path):
    (tmp_path / "stats").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "20261116-1130-twisters.json").write_text(json.dumps({
        "id": "20261116-1130-twisters", "status": "done",
        "title": "Glen Powell's aura | Twisters | #edit",
        "hook": "He chases what everyone else fears",
        "music": "sw_worthnothingfeatolivertr.mp3",
        "tags": ["twisters", "movie"],
    }))
    (tmp_path / "stats" / "latest.json").write_text(json.dumps({
        "videos": {
            "v1": {"title": "Glen Powell's aura | Twisters | #edit", "views": 4545, "likes": 80,
                   "queue_id": "20261116-1130-twisters", "published": "2026-11-16T06:00:00+00:00",
                   "hook": "He chases what everyone else fears", "music": "sw_worthnothingfeatolivertr.mp3"},
            "v2": {"title": "Quiet one", "views": 20, "likes": 0, "queue_id": "other",
                   "published": "2026-11-16T06:00:00+00:00"},
        }
    }))
    now = datetime(2026, 11, 17, 21, 0, tzinfo=IST)
    report = weekly_hits.write_weekly_hits(tmp_path, now=now)
    assert report["n"] == 2
    assert report["top_titles"][0]["name"] == "twisters"
    assert report["top_hooks"][0]["name"] == "He chases what everyone else fears"
    assert report["top_songs"][0]["name"] == "sw_worthnothingfeatolivertr.mp3"
    assert report["weights"]["songs"]["sw_worthnothingfeatolivertr.mp3"] > 0
    assert (tmp_path / "stats" / "weekly_hits.json").exists()

    data = perf.load_perf(tmp_path)
    hit = perf.song_bonus(data, {"file": "sw_worthnothingfeatolivertr.mp3", "title": "Worth Nothing", "artist": "X"})
    miss = perf.song_bonus(data, {"file": "unknown.mp3", "title": "Nope", "artist": "Y"})
    assert hit > miss
    assert perf.title_bonus(data, "twisters") > perf.title_bonus(data, "unknownslug")


def test_weekly_hits_ignores_older_than_a_week(tmp_path: Path):
    (tmp_path / "stats").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "stats" / "latest.json").write_text(json.dumps({
        "videos": {
            "old": {"title": "Old hit", "views": 99999, "published": "2026-10-01T00:00:00+00:00",
                    "queue_id": "20261001-0900-oldhit", "hook": "Old hook", "music": "old.mp3"},
            "new": {"title": "New modest", "views": 50, "published": "2026-11-16T00:00:00+00:00",
                    "queue_id": "20261116-0900-newmodest", "hook": "New hook", "music": "new.mp3"},
        }
    }))
    now = datetime(2026, 11, 17, 21, 0, tzinfo=IST)
    report = weekly_hits.build_weekly_hits(tmp_path, now=now)
    names = {t["name"] for t in report["top_titles"]}
    assert "newmodest" in names or "new" in {t["name"] for t in report["top_songs"]}
    assert "old.mp3" not in {s["name"] for s in report["top_songs"]}
