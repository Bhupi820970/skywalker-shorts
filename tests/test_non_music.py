"""Talks / podcasts / audiobooks never reach the edit-music picker (20261121 Mark Tilbury pick)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import music_lib

TILBURY = {
    "file": "pr_zzdrsxx3zgy.mp3", "artist": "Mark Tilbury", "priority": "high",
    "title": "I Built A Business From Scratch Only Following Claude’s Advice", "duration_s": 1674.1,
    "source_url": "https://www.youtube.com/watch?v=ZzDRSxx3zgY",
}


def test_business_talk_is_denied_even_as_priority():
    assert music_lib.non_music_reason(TILBURY)
    assert music_lib.is_denied_track(TILBURY)
    # a re-download under another file name is still caught by the keywords
    assert music_lib.is_denied_track(dict(TILBURY, file="pr_new.mp3", artist="Someone"))


def test_spoken_word_keywords_and_signals():
    for title in ("Joe Rogan Podcast #2001", "Full Interview with the director", "Motivational speech 2026",
                  "How I made money with ChatGPT", "Les Misérables audiobook", "Cyberpunk Librarian Episode 57"):
        assert music_lib.is_non_music_track({"title": title}), title
    assert music_lib.is_non_music_track({"title": "x", "source_id": "girl_boat_1109_librivox"})
    assert music_lib.is_non_music_track({"title": "Some Upload", "categories": ["People & Blogs"]})
    assert music_lib.is_non_music_track({"title": "Long Talk", "duration_s": 1800})


def test_real_songs_and_long_mixes_stay():
    for t in (
        {"title": "MONTAGEM CORAL (Slowed)", "artist": "DJ Holanda", "duration_s": 120},
        {"title": "AURA 1 HOUR AURA MUSIC PLAYLIST 2025 EMPIRE PHONK", "duration_s": 3600},
        {"title": "Moon River (instrumental) - Henry Mancini", "duration_s": 233, "categories": ["Music"]},
        {"title": "Siegmund's love song", "artist": "tollefsen trio"},
        {"title": "Francis Lai - Theme From Love Story (Finale)", "priority": "high"},
    ):
        assert not music_lib.is_non_music_track(t), t


def test_library_keeps_all_priority_songs_but_tilbury():
    lib = json.loads((ROOT / "music" / "library.json").read_text())
    bad = [t["file"] for t in lib["tracks"] if music_lib.is_priority(t) and music_lib.is_non_music_track(t)]
    assert bad == ["pr_zzdrsxx3zgy.mp3"]
