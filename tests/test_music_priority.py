"""Owner priority tracks, genre categories, Gemini song/drop validation, drop analysis."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import music_lib
import add_priority_music as apm


def _lib(tmp_path, tracks):
    (tmp_path / "music" / "lib").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "music_denylist.json").write_text(json.dumps({"entries": [
        {"title": "Goofy Ahh Phonk"}, {"artist": "187_NOTOR1OUS_ENEMY"}, {"title": "Ailow - Godless", "artist": "Ailow"}]}))
    for t in tracks:
        (tmp_path / "music" / "lib" / t["file"]).write_bytes(b"x")
    (tmp_path / "music" / "library.json").write_text(json.dumps({"dir": "music/lib", "tracks": tracks}))
    music_lib._DENY_CACHE = None
    return tmp_path


def T(file, cat=None, prio=False, **kw):
    meta = dict(apm.CAT_META.get(cat or "action"))
    t = {"file": file, "title": file, "artist": "a", "genre": meta["genre"], "mood": meta["mood"],
         "tags": list(meta["tags"]), "pillars": list(meta["pillars"]), "bpm": 100, "drop_t": 10.0,
         "safety": "safe", "duration_s": 120}
    if cat:
        t["cat"] = cat
    if prio:
        t["priority"] = "high"
    t.update(kw)
    return t


TRACKS = [
    T("phonk_a.mp3", "action"), T("phonk_b.mp3", "action"), T("prio_phonk.mp3", "action", True),
    T("love_a.mp3", "love"), T("prio_love.mp3", "love", True), T("cine.mp3", "cinematic"),
    T("hh.mp3", "hiphop"), T("goofy.mp3", "action", True, title="Goofy Ahh Phonk"),
]


def test_track_cat_derivation():
    assert music_lib.track_cat({"genre": "drift phonk"}) == "action"
    assert music_lib.track_cat({"genre": "romantic", "mood": "romantic"}) == "love"
    assert music_lib.track_cat({"genre": "hip-hop"}) == "hiphop"
    assert music_lib.track_cat({"genre": "cinematic", "mood": "epic"}) == "cinematic"
    assert music_lib.track_cat({"genre": "phonk", "cat": "love"}) == "love"
    assert music_lib.track_cat({"genre": "electronic", "title": "Resonant_Therapy"}) == "action"


def test_priority_wins_for_action_and_love(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_MUSIC_GENRE", raising=False)
    monkeypatch.setattr(music_lib.perf, "load_perf", lambda AE: {})
    AE = _lib(tmp_path, TRACKS)
    for seed in ("s1", "s2", "s3"):
        assert music_lib.pick(AE, "anime", seed)["file"] == "prio_phonk.mp3"
        assert music_lib.pick(AE, "romance", seed, text="love story")["file"] == "prio_love.mp3"


def test_non_repeat_falls_to_next_and_never_banned(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_MUSIC_GENRE", raising=False)
    monkeypatch.setattr(music_lib.perf, "load_perf", lambda AE: {})
    AE = _lib(tmp_path, TRACKS)
    t = music_lib.pick(AE, "anime", "s", exclude=["prio_phonk.mp3"])
    assert t["file"] not in ("prio_phonk.mp3", "goofy.mp3")
    lt = music_lib.pick(AE, "romance", "s", exclude=["prio_love.mp3"], text="love")
    assert music_lib.track_cat(lt) == "love"


def test_forced_genre_per_demo(tmp_path, monkeypatch):
    monkeypatch.setattr(music_lib.perf, "load_perf", lambda AE: {})
    AE = _lib(tmp_path, TRACKS)
    for g, want in (("cinematic", "cine.mp3"), ("hiphop", "hh.mp3"), ("love", "prio_love.mp3"), ("action", "prio_phonk.mp3")):
        monkeypatch.setenv("SKY_MUSIC_GENRE", g)
        assert music_lib.pick(AE, "anime", "x")["file"] == want


def test_gemini_wrong_category_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_MUSIC_GENRE", raising=False)
    monkeypatch.setattr(music_lib.perf, "load_perf", lambda AE: {})
    AE = _lib(tmp_path, TRACKS)
    t = music_lib.pick(AE, "romance", "x", prefer="phonk_a.mp3", text="love")
    assert t["file"] == "prio_love.mp3" and t["picked_by"] == "hash"
    t = music_lib.pick(AE, "anime", "x", prefer="phonk_b.mp3")
    assert t["file"] == "phonk_b.mp3" and t["picked_by"] == "gemini"


def test_catalog_lists_priority_first_and_hides_banned(tmp_path, monkeypatch):
    monkeypatch.delenv("SKY_MUSIC_GENRE", raising=False)
    AE = _lib(tmp_path, TRACKS)
    cat = music_lib.catalog(AE, mood="action")
    assert cat[0]["file"] == "prio_phonk.mp3" and cat[0]["priority"] == "high"
    assert "goofy.mp3" not in {r["file"] for r in cat}
    love = music_lib.catalog(AE, mood="love")
    assert love[0]["file"] == "prio_love.mp3"
    assert all(r["cat"] == "love" for r in love)


def test_valid_drop_snaps_to_analysed_points():
    t = {"drop_t": 30.0, "highs": [30.0, 62.5], "duration_s": 120}
    assert music_lib.valid_drop(t, 62.0) == 62.5
    assert music_lib.valid_drop(t, 45.0) is None
    assert music_lib.valid_drop(t, 118.0) is None


def test_high_points_find_the_drop():
    sr = 8000
    quiet = 0.05 * np.sin(np.linspace(0, 2000, sr * 20))
    loud = 0.8 * np.sin(np.linspace(0, 2000, sr * 20))
    y = np.concatenate([quiet, loud, quiet, loud, quiet]).astype(np.float32)
    hp = apm.high_points(y, sr, [])
    assert hp and abs(hp[0] - 20.0) < 1.5 or abs(hp[0] - 60.0) < 1.5
    assert any(abs(h - 20.0) < 1.5 for h in hp) and any(abs(h - 60.0) < 1.5 for h in hp)


def test_build_row_is_priority_and_guess_cat():
    row = apm.build_row("pr_x.mp3", {"url": "u", "cat": "auto", "title": "Romantic piano love theme"}, {},
                        {"bpm": 80, "drop_t": 20, "highs": [20], "duration_s": 150, "energy": 0.4})
    assert row["priority"] == "high" and row["cat"] == "love" and music_lib.track_cat(row) == "love"
    assert apm.guess_cat("BRAZILIAN PHONK MONTAGEM") == "action"
    assert apm.guess_cat("Boom bap hip hop beat") == "hiphop"
