"""Automatic footage refill (25 Nov): fresh = unposted clips; refill asks for new scenes when short."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import clip_fetch  # noqa: E402
import footage_refill as fr  # noqa: E402


def _w(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def _root(tmp_path: Path, posted: list[str]) -> Path:
    _w(tmp_path / "config" / "posted_history.json",
       {"items": [{"id": "old", "at": "2026-11-20", "keys": [f"clip:{c}" for c in posted]}]})
    _w(tmp_path / "config" / "topic_catalog.json", {"items": [{"slug": "dandadan", "hook": "Why Okarun cannot look away"}]})
    _w(tmp_path / "stats" / "latest.json", {"videos": {
        "a": {"queue_id": "20261124-2230-arcane2", "views": 13465},
        "b": {"queue_id": "20261123-0630-dandadanlove", "views": 5014}}})
    return tmp_path


def test_threshold_is_seven_days_plus_ten_backup():
    assert fr.threshold(7, 10) == 7 * fr.slots_per_day() + 10


def test_posted_clips_do_not_count_as_fresh(tmp_path):
    root = _root(tmp_path, ["dandadan_scene1_h264.mp4", "dandadan_scene2_h264.mp4"])
    assets = {"dandadan_scene1_h264.mp4", "dandadan_scene2_h264.mp4", "dandadan_scene3_h264.mp4",
              "arcane_scene1_h264.mp4", "oppenheimer_trailer_h264.mp4"}
    inv = fr.inventory(fr.fresh_assets(assets, root))
    assert inv == {"dandadan": 1, "arcane": 1}


def test_short_on_footage_plans_requests_with_seed_fallback(tmp_path):
    root = _root(tmp_path, [])
    st = fr.build_plan(root, {"rrr_scene1_h264.mp4"}, use_gemini=False)
    assert st["deficit_videos"] == st["threshold_videos"]
    assert st["source"] == "seeds"
    slugs = st["requests"]
    assert slugs and "arcane" in slugs[:4] and "dandadan" in slugs[:4]  # channel winners first
    for r in st["doc"]["requests"]:
        assert r["refill"] and r["prefer_english"]
        assert not any(fr.TRAILER.search(q) for q in r["search_queries"])


def test_gemini_429_falls_back_to_seeds(tmp_path, monkeypatch):
    root = _root(tmp_path, [])
    monkeypatch.setattr(fr, "gemini_requests", lambda *a, **k: [])
    st = fr.build_plan(root, set(), use_gemini=True)
    assert st["source"] == "seeds" and st["requests"]


def test_enough_footage_requests_nothing(tmp_path, monkeypatch):
    root = _root(tmp_path, [])
    monkeypatch.setattr(fr, "threshold", lambda *a, **k: 2)
    monkeypatch.setattr(fr, "MIN_CLIPS", 4)   # clip-pool rule off for this video-rule test
    assets = {f"t{i}_scene{j}_h264.mp4" for i in range(3) for j in range(1, 3)}
    st = fr.build_plan(root, assets, use_gemini=False)
    assert st["deficit_videos"] == 0 and "doc" not in st


def test_unknown_release_never_plans(tmp_path):
    st = fr.build_plan(_root(tmp_path, []), None, use_gemini=False)
    assert "doc" not in st and not st["release_known"]


def test_clean_request_blocks_nsfw_piracy_and_fan_channels():
    allowed = ["Netflix Anime", "TOHO animation"]
    assert fr.clean_request({"words": "ANIME ECCHI FANSERVICE"}, allowed, {}) is None
    assert fr.clean_request({"words": "X", "search_queries": ["x full episode free download"]}, allowed, {}) is None
    r = fr.clean_request({"slug": "Dan Da Dan!", "words": "dan da dan",
                          "official_channels": ["Netflix Anime", "AnimeEditsHD fan channel"],
                          "search_queries": ["dandadan trailer", "dandadan clip netflix anime"],
                          "links": ["https://x.com/NetflixAnime/status/123456789", "https://bit.ly/x"]}, allowed, {})
    assert r["slug"] == "dandadan" and r["official_channels"] == ["Netflix Anime"]
    assert r["search_queries"] == ["dandadan clip netflix anime"]
    assert r["links"] == ["https://x.com/NetflixAnime/status/123456789"]


def test_clip_fetch_counts_only_unposted_scenes():
    have = {"dandadan_scene1_h264.mp4", "dandadan_scene2_h264.mp4", "dandadan_scene3_h264.mp4", "arcane_scene1_h264.mp4"}
    blocked = {"dandadan_scene1_h264.mp4", "dandadan_scene2_h264.mp4"}
    assert clip_fetch.fresh_scene_count("dandadan", have, blocked) == 1
    assert clip_fetch.fresh_scene_count("dandadan", have, None) == 3


def test_clip_fetch_stops_after_repeated_rate_limits(monkeypatch, tmp_path):
    class R:
        returncode = 1
        stdout = ""
        stderr = "ERROR: HTTP Error 429: Too Many Requests"
    monkeypatch.setattr(clip_fetch, "run", lambda *a, **k: R())
    monkeypatch.setattr(clip_fetch, "WORK", tmp_path)
    clip_fetch._RL["hits"] = 0
    for _ in range(clip_fetch.RL_MAX):
        assert not clip_fetch.download("https://www.youtube.com/watch?v=abc", tmp_path / "a_scene1_h264.mp4")
    assert clip_fetch.rate_limited()
    clip_fetch._RL["hits"] = 0


def test_nsfw_titles_are_filtered():
    assert clip_fetch.NSFW.search("Best fanservice moments")
    assert not clip_fetch.NSFW.search("Okarun and Momo | DAN DA DAN | Clip | Netflix Anime")


def test_refill_clips_register_in_backup_pool(monkeypatch, tmp_path):
    pool = tmp_path / "backup_pool.json"
    _w(pool, {"items": [{"id": "backup-arcane", "scene_assets": ["arcane_scene1_h264.mp4"], "fallback_mp4": []}]})
    monkeypatch.setattr(clip_fetch, "POOL", pool)
    req = {"slug": "dandadan", "words": "DAN DA DAN", "hook": "Why Okarun cannot look away", "refill": True}
    clip_fetch.register_pool([{"req": req, "asset": "dandadan_scene7_h264.mp4", "url": "https://x.com/a/status/1"},
                              {"req": {"slug": "arcane", "refill": True}, "asset": "arcane_scene2_h264.mp4"}], {})
    items = {b["id"]: b for b in json.loads(pool.read_text())["items"]}
    assert items["backup-dandadan"]["scene_assets"] == ["dandadan_scene7_h264.mp4"]
    assert items["backup-dandadan"]["template"] == "whitebar"
    assert items["backup-arcane"]["scene_assets"] == ["arcane_scene1_h264.mp4", "arcane_scene2_h264.mp4"]


def test_refill_workflow_is_isolated_from_renders():
    y = (ROOT / ".github" / "workflows" / "refill.yml").read_text()
    assert "group: footage-refill" in y and "cancel-in-progress: false" in y
    assert "push:" not in y and "render" not in y.split("jobs:")[1].split("Summary")[0].replace("renders", "")



def test_clip_pool_refills_at_50_or_below(tmp_path, monkeypatch):
    root = _root(tmp_path, [])
    monkeypatch.setattr(fr, "threshold", lambda *a, **k: 2)          # video rule satisfied
    assets = {f"t{i}_scene{j}_h264.mp4" for i in range(25) for j in range(1, 3)}   # 50 fresh clips
    st = fr.build_plan(root, assets, use_gemini=False)
    assert st["fresh_clips"] == 50 and st["deficit_clips"] == 50
    assert st["deficit_videos"] == 25 and st["requests"]


def test_clip_pool_above_50_needs_nothing(tmp_path, monkeypatch):
    root = _root(tmp_path, [])
    monkeypatch.setattr(fr, "threshold", lambda *a, **k: 2)
    assets = {f"t{i}_scene{j}_h264.mp4" for i in range(17) for j in range(1, 4)}   # 51 fresh clips
    st = fr.build_plan(root, assets, use_gemini=False)
    assert st["fresh_clips"] == 51 and st["deficit_clips"] == 0 and st["deficit_videos"] == 0
