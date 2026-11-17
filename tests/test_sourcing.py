"""Source-chain unit tests (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sourcing


def test_rank_prefers_x_then_youtube_then_release():
    x = "https://x.com/NetflixAnime/status/123"
    yt = "https://www.youtube.com/watch?v=abc"
    studio = "https://www.marvel.com/movies/foo"
    gh = "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/x.mp4"
    assert sourcing._rank_url(x) < sourcing._rank_url(yt) < sourcing._rank_url(studio) < sourcing._rank_url(gh)
    assert sourcing.is_x_url(x) and sourcing.is_x_url("https://twitter.com/TOHO_official/status/9")
    assert sourcing.is_youtube_url(yt)
    assert sourcing.is_release_url(gh)


def test_build_candidate_urls_orders_x_and_youtube_before_release():
    item = {
        "id": "20261114-0900-runningman",
        "words": "RUNNING MAN",
        "source_url": "https://www.youtube.com/watch?v=KD18ddeFuyM",
        "content_kind": "best scene",
        "fallback_mp4": [
            "https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/runningman_scene1_h264.mp4"
        ],
        "x_urls": ["https://x.com/Netflix/status/555"],
        "alt_source_urls": [
            "https://www.dailymotion.com/video/x9u83ec",
            "https://www.youtube.com/watch?v=other",
        ],
    }
    urls = sourcing.build_candidate_urls(item)
    assert urls[0].startswith("https://x.com/")
    gh_idxs = [i for i, u in enumerate(urls) if "github.com" in u]
    yt_idxs = [i for i, u in enumerate(urls) if "youtube.com" in u or "youtu.be" in u]
    x_idxs = [i for i, u in enumerate(urls) if sourcing.is_x_url(u)]
    assert x_idxs and yt_idxs and gh_idxs
    assert max(x_idxs) < min(yt_idxs) < min(gh_idxs)


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
         "channel": "TOHO animation", "handle": "", "duration": 5},
        {"url": "https://www.youtube.com/watch?v=twelveclip", "title": "Frieren official scene",
         "channel": "TOHO animation", "handle": "", "duration": 12},
    ]
    urls = sourcing.more_official_scene_urls(item, already=[], entries=entries)
    assert urls == [
        "https://www.youtube.com/watch?v=officialok1",
        "https://www.youtube.com/watch?v=twelveclip",
    ]


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
    assert sourcing.max_source_seconds() == 180.0
    monkeypatch.setenv("SKY_MAX_SOURCE_S", "120")
    assert sourcing.max_source_seconds() == 120.0


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


def test_more_official_accepts_gkids_and_comix_wave():
    item = {"words": "YOUR NAME", "pillar": "romance",
            "official_channels": ["GKIDS Films", "CoMix Wave Films"]}
    entries = [
        {"url": "https://www.youtube.com/watch?v=gkidsclip01", "title": "Your Name official scene",
         "channel": "GKIDS Films", "duration": 28},
        {"url": "https://www.youtube.com/watch?v=fanedit999", "title": "Your Name official scene",
         "channel": "Anime Recaps", "duration": 40},
        {"url": "https://www.youtube.com/watch?v=comixclip01", "title": "Kimi no Na wa official clip",
         "channel": "CoMix Wave Films", "duration": 35},
    ]
    urls = sourcing.more_official_scene_urls(item, already=[], entries=entries)
    assert urls == [
        "https://www.youtube.com/watch?v=gkidsclip01",
        "https://www.youtube.com/watch?v=comixclip01",
    ]


def test_more_official_seeds_catalog_urls_when_search_is_empty(monkeypatch):
    import subprocess
    class _R:
        returncode = 1
        stdout = ""
        stderr = "no"
    monkeypatch.setattr(sourcing.subprocess, "run", lambda *a, **k: _R())
    item = {"words": "YOUR NAME", "pillar": "romance"}
    already = ["https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/yourname_scene1_h264.mp4"]
    urls = sourcing.more_official_scene_urls(item, already=already, entries=None)
    assert any("Ja1q9DnLI6M" in u or "RuyHIkXdYf8" in u for u in urls)
    assert not any(sourcing.is_trailer_source(u) for u in urls)


def test_ytdlp_output_template_is_not_a_fixed_name():
    from pathlib import Path
    tmpl = sourcing.ytdlp_output_template(Path("/tmp/work/src.mp4"))
    assert tmpl.endswith(".%(ext)s")
    assert "src.%(ext)s" in tmpl
    assert tmpl != "/tmp/work/src.mp4"


def test_x_resolution_stats_counts_pass_fail_unknown():
    entries = [
        {"platform": "x", "url": "https://x.com/a/status/1", "width": 1920, "height": 1080},
        {"platform": "x", "url": "https://x.com/a/status/2", "width": 1280, "height": 720},
        {"platform": "x", "url": "https://x.com/a/status/3"},
        {"platform": "youtube", "url": "https://www.youtube.com/watch?v=z", "width": 1920, "height": 1080},
        {"url": "https://twitter.com/b/status/4", "formats": [{"width": 1920, "height": 1080}]},
    ]
    s = sourcing.x_resolution_stats(entries)
    assert s == {"x_total": 4, "x_pass_1080": 2, "x_below_1080": 1, "x_unknown": 1}
    assert sourcing.classify_resolution(1280, 720) == "fail"
    assert sourcing.classify_resolution(1920, 1080) == "pass"
    assert sourcing.classify_resolution(0, 0) == "unknown"
    # 1080p floor is not relaxed for X: formats_meet_1080 still rejects 720p
    assert sourcing.formats_meet_1080([], 1280, 720) is False


def test_official_x_handles_map_from_channels():
    hs = sourcing.official_x_handles_for({
        "words": "SUZUME",
        "official_channels": ["GKIDS Films", "TOHO animation", "Netflix Anime"],
        "x_handles": ["@NetflixAnime"],
    })
    low = {h.lower() for h in hs}
    assert "netflixanime" in low
    assert "gkidsfilms" in low
    assert "toho_animation" in low


def test_x_post_matches_title():
    assert sourcing.x_post_matches_title("Suzume official clip from the movie", "SUZUME")
    assert sourcing.x_post_matches_title("Your Name scene", "YOUR NAME")
    assert not sourcing.x_post_matches_title("unrelated sports clip", "SUZUME")


def test_more_official_orders_x_entries_ahead_of_youtube():
    item = {"id": "demo-v5-romance", "words": "SUZUME", "official_channels": ["GKIDS Films"]}
    entries = [
        {"url": "https://www.youtube.com/watch?v=aaaa1111", "title": "Suzume scene",
         "channel": "GKIDS Films", "handle": "", "duration": 40},
        {"url": "https://x.com/GKIDSfilms/status/99", "title": "Suzume scene",
         "channel": "GKIDS Films", "handle": "GKIDSfilms", "duration": 30},
    ]
    urls = sourcing.more_official_scene_urls(item, already=[], limit=5, entries=entries)
    assert urls[0].startswith("https://x.com/")
    assert any("youtube.com" in u for u in urls)


def test_x_search_url_is_from_account_and_title():
    url = sourcing.x_search_url("NetflixAnime", "Suzume")
    assert url.startswith("https://x.com/search?")
    assert "from%3ANetflixAnime" in url or "from:NetflixAnime" in url
    assert "Suzume" in url

