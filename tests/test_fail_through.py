"""Every QA/pipeline rejection must fall through: next clip → title → backup."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sourcing
import title_swap


# One representative error per rejection path (exact strings from production).
PATHS = {
    "footage_too_short": "pipeline/QA failed (exit 1): only 17.1s of unique clean shots (need ≥18s, aim 20-28s); not repeating shots and not publishing a short cut",
    "no_crop_letterbox": "pipeline/QA failed (exit 1): QA no_crop failed: {'ok': False, 'source': [1920, 1080], 'letterbox_free': [1920, 1080], 'plate': [1080, 608], 'letterbox_rows_black': True, 'shift_px_median': 3.87}",
    "logo_watermark": "pipeline/QA failed (exit 1): auto_qc: 4 frame(s) failed empty-dark/logo/text detectors",
    "resolution_under_1080": "download failed: reject 1280x720 below 1080p (short side 720): https://example/clip",
    "white_bar_caption": "pipeline/QA failed (exit 1): hook banner not visible often enough",
    "dialogue_leftover": "gemini QC reject: leftover dialogue and sound effects",
    "download_404": "download failed: HTTP Error 404: Not Found",
    "download_bot_check": "download failed: Sign in to confirm you’re not a bot",
    "music_missing": "pipeline/QA failed (exit 1): No credited music tracks found under music/",
    "publish_gate": "publish gate failed: auto_qc failed: 4 frame(s) failed empty-dark/logo/text detectors",
    "no_usable_scenes": "pipeline/QA failed (exit 1): No usable scenes after filtering",
}


def test_each_rejection_tries_next_source_then_title_then_backup():
    for name, reason in PATHS.items():
        assert title_swap.should_try_next_source(reason), name
        assert title_swap.should_swap(reason), name
        assert title_swap.recovery_action(
            reason, sources_left=True, titles_left=True, backups_left=True,
        ) == title_swap.NEXT_SOURCE, name
        assert title_swap.recovery_action(
            reason, sources_left=False, titles_left=True, backups_left=True,
        ) == title_swap.NEXT_TITLE, name
        assert title_swap.recovery_action(
            reason, sources_left=False, titles_left=False, backups_left=True,
        ) == title_swap.BACKUP, name
        assert title_swap.recovery_action(
            reason, sources_left=False, titles_left=False, backups_left=False,
        ) == title_swap.GIVE_UP, name


def test_runner_shutdown_does_not_abandon_the_title():
    reason = "The runner has received a shutdown signal. This can happen when the runner service is stopped."
    assert not title_swap.should_try_next_source(reason)
    assert not title_swap.should_swap(reason)
    assert title_swap.recovery_action(
        reason, sources_left=True, titles_left=True, backups_left=True,
    ) == title_swap.RETRY_SAME
    assert title_swap.recovery_action(
        "render job ended with no result JSON (timed out or cancelled mid-render)",
        sources_left=True, titles_left=True, backups_left=True,
    ) == title_swap.RETRY_SAME


def test_qc_skipped_stays_on_same_title_for_qc_retry():
    reason = "gemini QC skipped"
    assert not title_swap.should_try_next_source(reason)
    assert not title_swap.should_swap(reason)
    assert title_swap.recovery_action(
        reason, sources_left=True, titles_left=True, backups_left=True,
    ) == title_swap.RETRY_SAME


def test_youtube_watch_is_not_a_topic_slug():
    assert sourcing.source_topic_slug("https://www.youtube.com/watch?v=Pbu0f1JgzVw") == ""
    assert sourcing.source_topic_slug("watch") == ""
    assert sourcing.source_topic_slug("suzume_scene2_h264.mp4") == "suzume"


def test_fallback_prefers_same_format_then_any_backup(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "queue").mkdir()
    (tmp_path / "config" / "topic_catalog.json").write_text('{"items":[]}')
    (tmp_path / "config" / "source_catalog.json").write_text('{"topics":{}}')
    (tmp_path / "config" / "edit_formats.json").write_text('{"love":{"titles":[]}}')
    gh = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources"
    pool = [
        {
            "id": "backup-lazarus",
            "words": "LAZARUS",
            "slug": "lazarus",
            "edit_format": "action",
            "sources": [f"{gh}/lazarus_scene1_h264.mp4"],
            "fallback_mp4": [f"{gh}/lazarus_scene1_h264.mp4"],
            "fallback_asset": "lazarus_scene1_h264.mp4",
            "content_kind": "best scene",
        },
        {
            "id": "backup-yourname",
            "words": "YOUR NAME",
            "slug": "yourname",
            "edit_format": "love",
            "pillar": "romance",
            "sources": [f"{gh}/yourname_scene1_h264.mp4"],
            "fallback_mp4": [f"{gh}/yourname_scene1_h264.mp4"],
            "fallback_asset": "yourname_scene1_h264.mp4",
            "content_kind": "romance scene",
        },
    ]
    (tmp_path / "config" / "backup_pool.json").write_text(__import__("json").dumps({"items": pool}))
    (tmp_path / "queue" / "demo.json").write_text("[]")
    love = {
        "id": "demo-v8-romance",
        "edit_format": "love",
        "words": "SUZUME",
        "slug": "suzume",
        "sources": [f"{gh}/suzume_scene1_h264.mp4"],
        "content_kind": "romance scene",
    }
    same = title_swap.next_fallback_item(love, root=tmp_path, same_format_only=True)
    assert same is not None
    assert title_swap.title_slug(same) == "yourname"
    # After love titles are excluded, any official backup remains.
    any_item = title_swap.next_fallback_item(
        love, exclude={"yourname"}, root=tmp_path, same_format_only=False,
    )
    assert any_item is not None
    assert title_swap.title_slug(any_item) == "lazarus"
