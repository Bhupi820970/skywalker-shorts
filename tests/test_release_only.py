"""GitHub renders never download from YouTube/X: clips come only from the sources release."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import sourcing  # noqa: E402
import title_swap  # noqa: E402

REL = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/"


def _env(monkeypatch, assets):
    monkeypatch.setenv("SKY_RELEASE_ONLY", "1")
    monkeypatch.setattr(sourcing, "_RELEASE_ASSETS", {"sources": set(assets)})


def test_release_only_default_on_github(monkeypatch):
    monkeypatch.delenv("SKY_RELEASE_ONLY", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert sourcing.release_only()
    monkeypatch.setenv("GITHUB_ACTIONS", "")
    assert not sourcing.release_only()
    monkeypatch.setenv("SKY_RELEASE_ONLY", "0")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert not sourcing.release_only()


def test_ytdlp_never_runs_in_release_only(monkeypatch, tmp_path):
    _env(monkeypatch, [])
    called = []
    monkeypatch.setattr(sourcing.subprocess, "run", lambda *a, **k: called.append(a))
    ok, why = sourcing.download_ytdlp("https://www.youtube.com/watch?v=abcdefghijk", tmp_path / "x.mp4")
    assert not ok and "release-only" in why
    assert sourcing.more_official_scene_urls({"words": "X"}, already=[]) == []
    assert sourcing.clip_search_urls({"words": "X"}) == []
    assert not called


def test_candidates_are_release_urls_only(monkeypatch):
    _env(monkeypatch, [])
    item = {"source_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "x_urls": ["https://x.com/crunchyroll/status/123456789"],
            "fallback_mp4": [REL + "suzume_scene1_h264.mp4"], "words": "SUZUME"}
    urls = sourcing.build_candidate_urls(item)
    assert urls and all("/releases/download/" in u for u in urls)


def test_release_scene_assets(monkeypatch):
    names = {"suzume_scene1_h264.mp4", "suzume_scene3_h264.mp4", "suzume_trailer_h264.mp4", "x_scene1_h264.mp4"}
    assert sourcing.release_scene_assets("suzume", names) == ["suzume_scene1_h264.mp4", "suzume_scene3_h264.mp4"]


def _no_health(monkeypatch, health=None, block=None):
    monkeypatch.setattr(title_swap, "release_clip_health", lambda root=None, **k: dict(health or {}))
    monkeypatch.setattr(title_swap, "release_title_blocklist", lambda root=None: dict(block or {}))
    monkeypatch.setattr(title_swap, "used_today_slugs", lambda item, root=None: set())


def test_backup_title_uses_existing_release_clips(monkeypatch):
    _env(monkeypatch, ["suzume_scene1_h264.mp4", "suzume_scene2_h264.mp4", "arcane2_scene3_h264.mp4"])
    _no_health(monkeypatch)
    pool = [
        {"slug": "arcane2", "words": "ARCANE", "sources": [REL + "arcane2_scene3_h264.mp4"]},
        {"slug": "gone", "words": "GONE", "sources": [REL + "gone_scene1_h264.mp4"]},
        {"slug": "suzume", "words": "SUZUME", "sources": [REL + "suzume_scene9_h264.mp4"]},
    ]
    monkeypatch.setattr(title_swap, "_load_pool", lambda root=None: [dict(p) for p in pool])
    monkeypatch.setattr(title_swap, "recently_used_slugs", lambda root=None, **k: set())
    nxt = title_swap.next_fallback_item({"slug": "princessmononoke", "words": "MONONOKE"})
    assert nxt["scene_assets"] == ["suzume_scene1_h264.mp4", "suzume_scene2_h264.mp4"]
    assert all(u.startswith(REL) for u in nxt["sources"])


def test_fallback_skips_logo_burned_and_failing_clips(monkeypatch):
    """21 Nov: every fallback went to Pathaan (most clips) whose YRF logo fails the filter."""
    _env(monkeypatch, ["pathaan_scene1_h264.mp4", "pathaan_scene2_h264.mp4", "pathaan_scene3_h264.mp4",
                       "pathaan_scene4_h264.mp4", "bleach_scene1_h264.mp4", "bleach_scene2_h264.mp4",
                       "bleach_scene3_h264.mp4", "dandadan_scene1_h264.mp4", "dandadan_scene2_h264.mp4"])
    _no_health(monkeypatch, health={"bleach": {"ok": 0, "bad": 3}, "dandadan": {"ok": 2, "bad": 0}},
               block={"pathaan": "YRF logo"})
    pool = [{"slug": s_, "words": s_.upper(), "sources": []} for s_ in ("pathaan", "bleach", "dandadan")]
    monkeypatch.setattr(title_swap, "_load_pool", lambda root=None: [dict(p) for p in pool])
    monkeypatch.setattr(title_swap, "recently_used_slugs", lambda root=None, **k: {"dandadan"})
    nxt = title_swap.next_fallback_item({"id": "20261121-1130-drstone", "slug": "drstone", "words": "DR STONE"})
    assert title_swap.title_slug(nxt) == "dandadan"


def test_fallback_none_when_only_bad_clips(monkeypatch):
    _env(monkeypatch, ["pathaan_scene1_h264.mp4", "pathaan_scene2_h264.mp4"])
    _no_health(monkeypatch, block={"pathaan": "YRF logo"})
    monkeypatch.setattr(title_swap, "_load_pool",
                        lambda root=None: [{"slug": "pathaan", "words": "PATHAAN", "sources": []}])
    monkeypatch.setattr(title_swap, "recently_used_slugs", lambda root=None, **k: set())
    assert title_swap.next_fallback_item({"id": "x", "slug": "drstone", "words": "DR STONE"}) is None


def test_parallel_slots_spread_over_proven_titles(monkeypatch):
    _env(monkeypatch, ["aaa_scene1_h264.mp4", "aaa_scene2_h264.mp4", "bbb_scene1_h264.mp4", "bbb_scene2_h264.mp4"])
    _no_health(monkeypatch, health={"aaa": {"ok": 1, "bad": 0}, "bbb": {"ok": 1, "bad": 0}})
    pool = [{"slug": s_, "words": s_.upper(), "sources": []} for s_ in ("aaa", "bbb")]
    monkeypatch.setattr(title_swap, "_load_pool", lambda root=None: [dict(p) for p in pool])
    monkeypatch.setattr(title_swap, "recently_used_slugs", lambda root=None, **k: set())
    a = title_swap.next_fallback_item({"id": "20261121-1600-x", "slug": "x", "words": "X"})
    b = title_swap.next_fallback_item({"id": "20261121-1830-y", "slug": "y", "words": "Y"})
    assert {title_swap.title_slug(a), title_swap.title_slug(b)} == {"aaa", "bbb"}


def test_clip_health_counts_swapped_footage(tmp_path):
    (tmp_path / "results").mkdir()
    rows = [
        {"id": "20261121-1130-drstone", "status": "failed", "swapped_to": "pathaan", "at": "2026-11-21 10:00 IST",
         "detail": "time budget reached; last: pipeline/QA failed (exit 1): No usable scenes after filtering"},
        {"id": "20261121-0900-dandadan", "status": "done", "asset_url": "u", "at": "2026-11-21 08:00 IST"},
    ]
    for r in rows:
        (tmp_path / "results" / f"{r['id']}.json").write_text(json.dumps(r))
    from datetime import datetime
    h = title_swap.release_clip_health(tmp_path, now=datetime(2026, 11, 21, 12, tzinfo=title_swap.IST))
    assert h["pathaan"] == {"ok": 0, "bad": 1} and h["dandadan"]["ok"] == 1
    assert "drstone" not in h


def test_stock_refill_only_from_release_topics(monkeypatch):
    import backup_stock
    _env(monkeypatch, ["suzume_scene1_h264.mp4", "suzume_scene2_h264.mp4", "mashle_scene1_h264.mp4"])
    pool = [{"id": "backup-suzume", "fallback_mp4": [REL + "suzume_scene1_h264.mp4"]},
            {"id": "backup-mashle", "fallback_mp4": [REL + "mashle_scene1_h264.mp4"]},
            {"id": "backup-gone", "fallback_mp4": [REL + "gone_scene1_h264.mp4"]}]
    assert [backup_stock.pool_slug(b) for b in backup_stock.release_backed_pool(pool)] == ["suzume"]


def test_bot_check_never_retires_a_topic():
    import backup_stock
    err = "download failed: yt-dlp:ERROR: [youtube] x: Sign in to confirm you're not a bot"
    assert not backup_stock.is_retire_worthy_error(err, {"detail": err})
    assert backup_stock.is_retire_worthy_error("reject logo/watermark: gh:x", None)


def test_render_workflow_has_no_ytdlp():
    wf = (ROOT / ".github" / "workflows" / "render.yml").read_text()
    assert "yt-dlp[default]" not in wf and 'SKY_RELEASE_ONLY: "1"' in wf


def test_scene_requests_plan(monkeypatch):
    import scene_requests
    from datetime import datetime
    monkeypatch.setattr(sourcing, "load_catalog", lambda: {"topics": {}})
    doc = scene_requests.build(now=datetime.now(scene_requests.IST), have={"suzume_scene1_h264.mp4"})
    assert isinstance(doc["requests"], list)
    for r in doc["requests"]:
        assert r["search_queries"] and all("trailer" not in q.lower() for q in r["search_queries"])
