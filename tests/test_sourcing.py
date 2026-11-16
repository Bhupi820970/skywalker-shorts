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
