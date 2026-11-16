"""Banned tracks stay out of the library picker and re-ingest."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import music_lib


def test_pkonk_enemy_is_permanently_denied():
    assert music_lib.is_denied_track({
        "file": "sw_pkonkbetapkonkphenomenon.mp3",
        "title": "PKONK BETA - PKONKPHENOMENON",
        "artist": "187_NOTOR1OUS_ENEMY",
        "source_id": "phonk-beta-phonkphenomnnon",
    })
    # re-ingest under a new filename but same artist/title still matches
    assert music_lib.is_denied_track({
        "file": "sw_somethingnew.mp3",
        "title": "PKONK BETA - PKONKPHENOMENON",
        "artist": "187_NOTOR1OUS_ENEMY",
    })
    assert not music_lib.is_denied_track({
        "file": "sw_interworldrapturephonk.mp3",
        "title": "INTERWORLD RAPTURE (PHONK)",
        "artist": "InterWorld",
    })


def test_denied_song_is_not_in_library_or_catalog():
    lib = __import__("json").loads((ROOT / "music" / "library.json").read_text())
    files = {t.get("file") for t in lib.get("tracks") or []}
    assert "sw_pkonkbetapkonkphenomenon.mp3" not in files
    cat = music_lib.catalog(ROOT)
    assert all(t.get("file") != "sw_pkonkbetapkonkphenomenon.mp3" for t in cat)
    assert all("187_notor1ous_enemy" not in (t.get("artist") or "").lower() for t in cat)
