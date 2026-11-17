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


def test_hated_shorts_songs_are_permanently_denied():
    # Hoppers Short JQGOukoBahY — Goofy Ahh Phonk
    assert music_lib.is_denied_track({
        "file": "sw_goofyahhphonkfullversion.mp3",
        "title": "Goofy Ahh Phonk [Full Version]",
        "source_id": "goofy-ahh-phonk-full-version",
    })
    assert music_lib.is_denied_track({
        "file": "sw_newname.mp3",
        "title": "Goofy Ahh Phonk",
    })
    # Cliff Booth Short S8KPiIoYKRM — Ailow Godless
    assert music_lib.is_denied_track({
        "file": "ncs_godless.mp3",
        "title": "Godless",
        "artist": "Ailow",
    })
    assert music_lib.is_denied_track({
        "file": "sw_somethingelse.mp3",
        "title": "Ailow - Godless",
        "artist": "Ailow",
    })
    lib = __import__("json").loads((ROOT / "music" / "library.json").read_text())
    files = {t.get("file") for t in lib.get("tracks") or []}
    assert "sw_goofyahhphonkfullversion.mp3" not in files
    assert "ncs_godless.mp3" not in files
    credits = __import__("json").loads((ROOT / "music" / "credits.json").read_text())
    assert all(t.get("file") != "ncs_godless.mp3" for t in credits.get("tracks") or [])
    assert all("goofyahhphonk" not in (t.get("file") or "").lower() for t in (lib.get("tracks") or []))
    assert not (ROOT / "music" / "ncs_godless.mp3").exists()
