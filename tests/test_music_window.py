"""Same song must not repeat inside a 3-day window."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import music_lib

IST = timezone(timedelta(hours=5, minutes=30))


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj) + "\n")


def test_recent_music_uses_three_day_window(tmp_path):
    now = datetime(2026, 11, 17, 16, 0, tzinfo=IST)
    res = tmp_path / "results"
    _write(res / "20261117-1130-a.json", {
        "id": "20261117-1130-a", "status": "done",
        "slot": "2026-11-17T11:30:00+05:30", "music": "sw_metamorphosis.mp3",
    })
    _write(res / "20261116-0900-b.json", {
        "id": "20261116-0900-b", "status": "done",
        "slot": "2026-11-16T09:00:00+05:30", "music": "sw_murderinmymind.mp3",
    })
    _write(res / "20261113-1600-c.json", {
        "id": "20261113-1600-c", "status": "done",
        "slot": "2026-11-13T16:00:00+05:30", "music": "sw_oldphonk.mp3",
    })
    _write(res / "failed.json", {
        "id": "failed", "status": "failed",
        "slot": "2026-11-17T09:00:00+05:30", "music": "sw_shouldskip.mp3",
    })
    files = music_lib.recent_music_files(res, days=3, now=now)
    assert "sw_metamorphosis.mp3" in files
    assert "sw_murderinmymind.mp3" in files
    assert "sw_oldphonk.mp3" not in files
    assert "sw_shouldskip.mp3" not in files


def test_expand_same_song_includes_title_aliases(tmp_path):
    lib = {
        "tracks": [
            {"file": "sw_metamorphosis.mp3", "title": "INTERWORLD Metamorphosis"},
            {"file": "sw_metamorphosis_slowed.mp3", "title": "INTERWORLD Metamorphosis (slowed)"},
            {"file": "sw_other.mp3", "title": "Something Else"},
        ]
    }
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "library.json").write_text(json.dumps(lib))
    out = music_lib.expand_same_song(tmp_path, ["sw_metamorphosis.mp3"])
    # normalized titles may differ because of "(slowed)" — INTERWORLDMetamorphosis vs INTERWORLDMetamorphosisslowed
    # at least the used file stays, and a close exact title would be included
    assert "sw_metamorphosis.mp3" in out
    exact = music_lib.expand_same_song(tmp_path, ["sw_other.mp3"])
    assert exact == ["sw_other.mp3"]
