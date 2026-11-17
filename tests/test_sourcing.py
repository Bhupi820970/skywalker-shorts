"""Source-chain unit tests (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sourcing


def test_rank_prefers_github_and_studio_over_youtube():
    gh = "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/x.mp4"
    studio = "https://www.marvel.com/movies/foo"
    yt = "https://www.youtube.com/watch?v=abc"
    assert sourcing._rank_url(gh) < sourcing._rank_url(studio) < sourcing._rank_url(yt)


def test_build_candidate_urls_orders_fallback_before_youtube():
    item = {
        "id": "20261114-0900-runningman",
        "words": "RUNNING MAN",
        "source_url": "https://www.youtube.com/watch?v=KD18ddeFuyM",
        "content_kind": "best scene",
        "fallback_mp4": [
            "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/runningman_scene1_h264.mp4"
        ],
        "alt_source_urls": [
            "https://www.dailymotion.com/video/x9u83ec",
            "https://www.youtube.com/watch?v=other",
        ],
    }
    urls = sourcing.build_candidate_urls(item)
    assert urls[0].endswith("runningman_scene1_h264.mp4")
    yt_idxs = [i for i, u in enumerate(urls) if "youtube.com" in u or "youtu.be" in u]
    non_yt = [i for i, u in enumerate(urls) if "youtube.com" not in u and "youtu.be" not in u]
    assert non_yt and yt_idxs
    assert max(non_yt) < min(yt_idxs) or urls.index(item["fallback_mp4"][0]) == 0


def test_content_kinds_include_scenes_not_only_trailers():
    assert "best scene" in sourcing.CONTENT_KINDS
    assert "fight scene" in sourcing.CONTENT_KINDS
    assert "romance scene" in sourcing.CONTENT_KINDS
    item = {"id": "x-y-sololeveling", "words": "SOLO LEVELING", "pillar": "anime",
            "content_kind": "fight scene"}
    qs = sourcing.search_queries(item)
    assert any("fight" in q.lower() or "official clip" in q.lower() for q in qs)


def test_usable_false_blocks_the_catalog_asset():
    import sourcing
    names = sourcing.unusable_asset_names({"id": "demo-clips-bluelockisagi", "slug": "bluelockisagi"})
    assert "bluelockisagi_scene1_h264.mp4" in names
    assert sourcing.is_unusable_source("bluelockisagi_scene1_h264.mp4", {"id": "demo-clips-bluelockisagi", "slug": "bluelockisagi"})
    assert sourcing.is_unusable_source("jjkgojovstoji_scene1_h264.mp4", {"id": "demo-clips-jjkgojovstoji-v3", "slug": "jjkgojovstoji"})
    assert sourcing.is_unusable_source("https://www.youtube.com/watch?v=7LzDcr20SHc", {"id": "demo-clips-jjkgojovstoji-v3", "slug": "jjkgojovstoji"})


def test_topic_key_from_queue_id():
    assert sourcing.topic_key({"id": "20261114-0900-runningman"}) == "runningman"
    assert sourcing.topic_key({"id": "backup-superman"}) == "superman"


def test_trailers_are_never_candidates_or_queries():
    item = {
        "id": "20261120-0900-firforce3",
        "words": "FIRE FORCE",
        "content_kind": "trailer",
        "source_url": "https://www.youtube.com/watch?v=ThozbbykMdY",
        "fallback_asset": "firforce3_trailer_h264.mp4",
        "fallback_mp4": [
            "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/firforce3_scene1_h264.mp4",
            "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/firforce3_scene2_h264.mp4",
            "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/firforce3_trailer_h264.mp4",
        ],
    }
    assert sourcing.is_trailer_source("firforce3_trailer_h264.mp4")
    assert sourcing.is_trailer_source(item["fallback_mp4"][-1])
    assert not sourcing.is_trailer_source("firforce3_scene1_h264.mp4")
    urls = sourcing.build_candidate_urls(item)
    assert urls
    assert all(not sourcing.is_trailer_source(u) for u in urls)
    assert any("scene1" in u for u in urls)
    assert sourcing.item_has_scene_source(item)
    trailer_only = {
        "id": "20261117-0900-fnaf2",
        "fallback_asset": "fnaf2_trailer_h264.mp4",
        "fallback_mp4": [
            "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/fnaf2_trailer_h264.mp4"
        ],
        "content_kind": "trailer",
    }
    assert sourcing.build_candidate_urls(trailer_only) == []
    assert not sourcing.item_has_scene_source(trailer_only)
    qs = sourcing.search_queries({"id": "x-y-sololeveling", "words": "SOLO LEVELING",
                                  "pillar": "anime", "content_kind": "trailer"})
    blob = " ".join(qs).lower()
    assert "trailer" not in blob and "teaser" not in blob
    assert "clip" in blob or "scene" in blob


def test_fill_queue_topic_assets_skip_trailers():
    sys.path.insert(0, str(ROOT / "scripts"))
    import fill_queue
    topic = {
        "slug": "firforce3",
        "scene_assets": ["firforce3_scene1_h264.mp4", "firforce3_scene2_h264.mp4", "firforce3_scene3_h264.mp4"],
        "fallback_asset": "firforce3_trailer_h264.mp4",
    }
    names = fill_queue.topic_assets(topic, None)
    assert names == topic["scene_assets"]
    assert fill_queue.topic_assets({"slug": "fnaf2", "fallback_asset": "fnaf2_trailer_h264.mp4"}, None) == []


def test_unrecoverable_source_error():
    assert sourcing.is_unrecoverable_source_error("download failed: HTTP Error 404: Not Found")
    assert sourcing.is_unrecoverable_source_error("reject logo/watermark: gh:x/bluelock2_scene3_h264.mp4")
    assert sourcing.is_unrecoverable_source_error("Sign in to confirm you’re not a bot")
    assert not sourcing.is_unrecoverable_source_error("ffmpeg timed out mid-render")
    assert not sourcing.is_unrecoverable_source_error("HTTP 503 temporarily unavailable")


def test_trailer_result():
    assert sourcing.is_trailer_result({
        "source": "gh:https://github.com/x/releases/download/sources/wickedforgood_trailer_h264.mp4"
    })
    assert not sourcing.is_trailer_result({
        "source": "gh:https://github.com/x/releases/download/sources/dandadan_scene1_h264.mp4"
    })


def test_more_official_keeps_studio_skips_unofficial_and_trailers():
    item = {"words": "FRIEREN", "pillar": "anime", "official_channels": ["TOHO animation"]}
    entries = [
        {"url": "https://www.youtube.com/watch?v=officialok1", "title": "Frieren official scene",
         "channel": "TOHO animation", "handle": "", "duration": 40},
        {"url": "https://www.youtube.com/watch?v=fanclip99", "title": "Frieren fight no logo",
         "channel": "Anime Recaps", "handle": "", "duration": 45},
        {"url": "https://www.youtube.com/watch?v=trailer12", "title": "Frieren Official Trailer",
         "channel": "TOHO animation", "handle": "", "duration": 90},
        {"url": "https://www.youtube.com/watch?v=shortclip", "title": "Frieren official scene",
         "channel": "TOHO animation", "handle": "", "duration": 8},
    ]
    urls = sourcing.more_official_scene_urls(item, already=[], entries=entries)
    assert urls == ["https://www.youtube.com/watch?v=officialok1"]


def test_more_official_skips_already_used_and_needs_more_when_short():
    item = {"words": "KAIJU NO. 8", "pillar": "anime"}
    entries = [
        {"url": "https://www.youtube.com/watch?v=usedclip1", "title": "Official scene 1",
         "channel": "Netflix Anime", "duration": 50},
        {"url": "https://www.youtube.com/watch?v=newclip2", "title": "Official scene 2",
         "channel": "Netflix", "duration": 42},
    ]
    urls = sourcing.more_official_scene_urls(
        item, already=["https://www.youtube.com/watch?v=usedclip1"], entries=entries,
    )
    assert urls == ["https://www.youtube.com/watch?v=newclip2"]


def test_max_source_seconds_default_and_env(monkeypatch):
    monkeypatch.delenv("SKY_MAX_SOURCE_S", raising=False)
    assert sourcing.max_source_seconds() == 360.0
    monkeypatch.setenv("SKY_MAX_SOURCE_S", "180")
    assert sourcing.max_source_seconds() == 180.0


def test_is_below_1080_uses_short_side():
    assert sourcing.is_below_1080(1280, 720) is True
    assert sourcing.is_below_1080(1920, 1080) is False
    assert sourcing.is_below_1080(1080, 1920) is False
    assert sourcing.is_below_1080(3840, 2160) is False
    assert sourcing.is_below_1080(0, 0) is False  # unknown is not a reject


def test_formats_meet_1080_unknown_allowed_known_720_rejected():
    assert sourcing.formats_meet_1080([], 0, 0) is True
    assert sourcing.formats_meet_1080([], 1280, 720) is False
    assert sourcing.formats_meet_1080([{"width": 1920, "height": 1080}], 0, 0) is True
    assert sourcing.formats_meet_1080([{"height": 720}], 0, 0) is False
    # prefer the best listed format
    assert sourcing.formats_meet_1080(
        [{"width": 1280, "height": 720}, {"width": 1920, "height": 1080}], 0, 0
    ) is True
