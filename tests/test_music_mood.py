"""Romance/love titles pick emotional tracks; action titles keep phonk."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import music_lib
import edit_format


def _lib(tmp: Path, tracks: list[dict]) -> Path:
    libdir = tmp / "music" / "lib"
    libdir.mkdir(parents=True)
    (tmp / "music" / "library.json").write_text(json.dumps({"dir": "music/lib", "tracks": tracks}))
    for t in tracks:
        (libdir / t["file"]).write_bytes(b"x" * 100)
    return tmp


def _tracks():
    return [
        {"file": "sw_closeeyes.mp3", "title": "Close Eyes", "artist": "DVRST",
         "genre": "drift phonk", "mood": "dark-hype", "tags": ["phonk", "edit"],
         "pillars": ["anime", "movie"], "bpm": 110, "safety": "safe"},
        {"file": "sw_heartline.mp3", "title": "Quiet Evening", "artist": "Someone",
         "genre": "romance", "mood": "emotional", "tags": ["romance", "love", "edit"],
         "pillars": ["romance"], "bpm": 84, "safety": "safe"},
        {"file": "sw_sadpiano.mp3", "title": "Bittersweet Piano", "artist": "Someone",
         "genre": "romance", "mood": "sad", "tags": ["sad", "emotional"],
         "pillars": ["romance"], "bpm": 72, "safety": "safe"},
    ]


def test_is_love_track_skips_phonk_with_love_in_title():
    assert music_lib.is_love_track(_tracks()[1])
    assert music_lib.is_love_track(_tracks()[2])
    assert not music_lib.is_love_track(_tracks()[0])
    assert not music_lib.is_love_track({
        "title": "A Lovely Mango Brazilian Phonk Remix",
        "genre": "phonk", "mood": "dark-hype", "tags": ["phonk"], "pillars": ["anime"],
    })


def test_romance_pillar_picks_love_track(tmp_path):
    _lib(tmp_path, _tracks())
    picked = music_lib.pick(tmp_path, "romance", seed="demo-love")
    assert picked["file"] in ("sw_heartline.mp3", "sw_sadpiano.mp3")
    assert music_lib.is_love_track(picked)


def test_love_hook_keywords_pick_love_track_even_if_pillar_is_movie(tmp_path):
    _lib(tmp_path, _tracks())
    picked = music_lib.pick(tmp_path, "movie", seed="x", text="She already knew | a quiet almost-kiss")
    assert picked["file"] in ("sw_heartline.mp3", "sw_sadpiano.mp3")


def test_action_title_keeps_phonk(tmp_path):
    _lib(tmp_path, _tracks())
    picked = music_lib.pick(tmp_path, "anime", seed="demo-fight", text="When he goes all out")
    assert picked["file"] == "sw_closeeyes.mp3"


def test_infer_love_format_from_hook():
    assert edit_format.infer_format({"pillar": "romance"}) == "love"
    assert edit_format.infer_format({"hook": "They almost said it", "words": "BRIDGERTON"}) == "love"
    assert edit_format.infer_format({"pillar": "anime", "hook": "When Okarun goes all out"}) == "action"
