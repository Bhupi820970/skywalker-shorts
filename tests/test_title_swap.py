"""Same-format title auto-swap, including demo and forced-id items."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import title_swap


def _repo(tmp_path: Path, topics, pool=None, queue=None, sources=None) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "queue").mkdir()
    (tmp_path / "config" / "topic_catalog.json").write_text(json.dumps({"items": topics}))
    (tmp_path / "config" / "backup_pool.json").write_text(json.dumps({"items": pool or []}))
    (tmp_path / "config" / "source_catalog.json").write_text(json.dumps({"topics": sources or {}}))
    (tmp_path / "queue" / "demo.json").write_text(json.dumps(queue or []))
    # Point love-title detection at the real edit_formats (yourname/suzume/…).
    return tmp_path


def _scene(slug: str, n: int = 1) -> str:
    return f"https://github.com/chatgpt820970/skywalker-shorts/releases/download/sources/{slug}_scene{n}_h264.mp4"


def test_should_swap_on_short_footage_qc_logo_404_bot_but_not_cancel():
    assert title_swap.should_swap(
        "pipeline/QA failed (exit 1): only 13.6s of unique clean shots (need ≥18s, aim 20-28s)"
    )
    assert title_swap.should_swap("gemini QC reject: leftover dialogue and sound effects")
    assert title_swap.should_swap("reject logo/watermark: official clip with third-party logo")
    assert title_swap.should_swap("download failed: HTTP Error 404: Not Found")
    assert title_swap.should_swap("sign in to confirm you’re not a bot")
    assert title_swap.should_swap({"error": "no unused official source left for this topic"})
    # Mid-render runner death must retry the same title on the chunked path.
    assert not title_swap.should_swap(
        "The runner has received a shutdown signal. This can happen when the runner service is stopped."
    )
    assert not title_swap.should_swap("render job ended with no result JSON (timed out or cancelled mid-render)")
    assert not title_swap.should_swap("")


def test_romance_demo_swaps_to_next_official_romance_title(tmp_path):
    topics = [
        {
            "slug": "suzume",
            "pillar": "anime",
            "words": "SUZUME",
            "hook": "She kept walking back",
            "title": "She kept walking back | Suzume | #edit",
            "scene_assets": ["suzume_scene1_h264.mp4", "suzume_scene2_h264.mp4"],
            "content_kind": "romance scene",
            "edit_format": "love",
        },
        {
            "slug": "weatheringwithyou",
            "pillar": "anime",
            "words": "WEATHERING WITH YOU",
            "hook": "The sky followed them",
            "title": "The sky followed them | Weathering With You | #edit",
            "scene_assets": ["weatheringwithyou_scene1_h264.mp4", "weatheringwithyou_scene2_h264.mp4"],
            "content_kind": "romance scene",
            "edit_format": "love",
        },
        {
            "slug": "lazarus",
            "pillar": "anime",
            "words": "LAZARUS",
            "hook": "Axel has thirty days to run",
            "title": "Axel has thirty days to run | Lazarus | #edit",
            "scene_assets": ["lazarus_scene2_h264.mp4"],
            "content_kind": "best scene",
        },
    ]
    repo = _repo(tmp_path, topics)
    item = {
        "id": "demo-v5-romance",
        "demo": True,
        "no_publish": True,
        "edit_format": "love",
        "pillar": "romance",
        "words": "SUZUME",
        "hook": "She kept walking back",
        "title": "She kept walking back | Suzume | #edit",
        "sources": [_scene("suzume"), _scene("suzume", 2)],
        "fallback_mp4": [_scene("suzume"), _scene("suzume", 2)],
        "fallback_asset": "suzume_scene1_h264.mp4",
        "content_kind": "romance scene",
        "chunked": False,
    }
    assert title_swap.item_format(item) == "love"
    nxt = title_swap.next_eligible_title(item, root=repo)
    assert nxt is not None
    assert title_swap.title_slug(nxt) == "weatheringwithyou"
    assert title_swap.item_format(nxt) == "love"
    # Never pick an action title for a romance demo.
    assert title_swap.title_slug(nxt) != "lazarus"

    title_swap.apply_title_swap(item, nxt, reason="only 13.6s of unique clean shots")
    assert item["id"] == "demo-v5-romance"
    assert item["demo"] is True
    assert item["no_publish"] is True
    assert item["words"] == "WEATHERING WITH YOU"
    assert "weatheringwithyou_scene1_h264.mp4" in " ".join(item["sources"])
    assert item["edit_format"] == "love"
    assert item["pillar"] == "romance"
    assert item["edit_mode"] == "music_only"  # no dialogue anywhere
    assert item["title_swaps"][0]["from"] == "suzume"
    assert item["title_swaps"][0]["to"] == "weatheringwithyou"


def test_forced_action_item_does_not_swap_into_romance(tmp_path):
    topics = [
        {
            "slug": "lazarus",
            "words": "LAZARUS",
            "hook": "Axel has thirty days to run",
            "scene_assets": ["lazarus_scene2_h264.mp4"],
            "pillar": "anime",
            "content_kind": "best scene",
        },
        {
            "slug": "dandadanevil",
            "words": "DAN DA DAN",
            "hook": "Evil Eye was a lonely kid",
            "scene_assets": ["dandadanevil_scene1_h264.mp4", "dandadanevil_scene2_h264.mp4"],
            "pillar": "anime",
            "content_kind": "best scene",
        },
        {
            "slug": "suzume",
            "words": "SUZUME",
            "hook": "She kept walking back",
            "scene_assets": ["suzume_scene1_h264.mp4"],
            "pillar": "romance",
            "edit_format": "love",
            "content_kind": "romance scene",
        },
    ]
    repo = _repo(tmp_path, topics)
    item = {
        "id": "demo-v5-lazarus",
        "demo": True,
        "no_publish": True,
        "words": "LAZARUS",
        "hook": "Axel has thirty days to run",
        "sources": [_scene("lazarus", 2)],
        "pillar": "anime",
        "content_kind": "best scene",
    }
    assert title_swap.item_format(item) == "action"
    nxt = title_swap.next_eligible_title(item, root=repo)
    assert nxt is not None
    assert title_swap.title_slug(nxt) == "dandadanevil"
    assert title_swap.item_format(nxt) == "action"
    title_swap.apply_title_swap(item, nxt, reason="HTTP Error 404")
    assert item["id"] == "demo-v5-lazarus"
    assert item["edit_mode"] == "music_only"
    assert item["words"] == "DAN DA DAN"
    assert "suzume" not in " ".join(item.get("sources") or []).lower()


def test_skips_trailer_only_and_already_tried_titles(tmp_path):
    topics = [
        {
            "slug": "yourname",
            "words": "YOUR NAME",
            "hook": "He forgot her name again",
            "fallback_asset": "yourname_trailer_h264.mp4",
            "edit_format": "love",
            "pillar": "romance",
        },
        {
            "slug": "suzume",
            "words": "SUZUME",
            "hook": "She kept walking back",
            "scene_assets": ["suzume_scene1_h264.mp4"],
            "edit_format": "love",
            "pillar": "romance",
        },
    ]
    repo = _repo(tmp_path, topics)
    item = {
        "id": "demo-romance-yourname",
        "demo": True,
        "no_publish": True,
        "edit_format": "love",
        "pillar": "romance",
        "words": "YOUR NAME",
        "hook": "He forgot her name again",
        "sources": [_scene("yourname")],  # this item itself has a scene clip
        "content_kind": "romance scene",
    }
    # Trailer-only catalog entry is not eligible; suzume is.
    nxt = title_swap.next_eligible_title(item, root=repo)
    assert nxt is not None
    assert title_swap.title_slug(nxt) == "suzume"
    # Once suzume is excluded, nothing remains (trailer-only yourname twin is not used).
    none = title_swap.next_eligible_title(item, exclude={"suzume"}, root=repo)
    assert none is None


def test_apply_keeps_forced_id_and_records_history():
    item = {
        "id": "forced-romance",
        "slot": "2026-11-18T13:30:00+05:30",
        "demo": True,
        "no_publish": True,
        "chunked": True,
        "words": "SUZUME",
        "slug": "suzume",
        "hook": "old",
        "sources": [_scene("suzume")],
        "edit_format": "love",
    }
    cand = {
        "slug": "weatheringwithyou",
        "words": "WEATHERING WITH YOU",
        "hook": "The sky followed them",
        "title": "The sky followed them | Weathering With You | #edit",
        "sources": [_scene("weatheringwithyou")],
        "fallback_mp4": [_scene("weatheringwithyou")],
        "edit_format": "love",
        "pillar": "romance",
        "content_kind": "romance scene",
    }
    title_swap.apply_title_swap(item, cand, reason="QC reject")
    assert item["id"] == "forced-romance"
    assert item["slot"] == "2026-11-18T13:30:00+05:30"
    assert item["chunked"] is True
    assert item["no_publish"] is True
    assert item["swapped_from"] == "suzume"
    assert item["swapped_to"] == "weatheringwithyou"


def test_emotional_action_hook_is_not_treated_as_romance(tmp_path):
    """Frieren's 'she loved' hook must not steal a romance demo slot."""
    topics = [
        {
            "slug": "frieren",
            "pillar": "anime",
            "words": "FRIEREN",
            "hook": "She outlived everyone she loved",
            "scene_assets": ["frieren_scene1_h264.mp4"],
            "content_kind": "best scene",
        },
        {
            "slug": "weatheringwithyou",
            "words": "WEATHERING WITH YOU",
            "hook": "The sky followed them",
            "scene_assets": ["weatheringwithyou_scene1_h264.mp4"],
            # listed in edit_formats.json love titles; no explicit edit_format
            "pillar": "anime",
            "content_kind": "best scene",
        },
    ]
    repo = _repo(tmp_path, topics)
    frieren = {
        "id": "stock-frieren-01",
        "words": "FRIEREN",
        "hook": "She outlived everyone she loved",
        "sources": [_scene("frieren")],
        "pillar": "anime",
        "content_kind": "best scene",
    }
    assert title_swap.item_format(frieren) == "action"
    love = {
        "id": "demo-v5-romance",
        "demo": True,
        "no_publish": True,
        "edit_format": "love",
        "pillar": "romance",
        "words": "SUZUME",
        "sources": [_scene("suzume")],
        "content_kind": "romance scene",
    }
    nxt = title_swap.next_eligible_title(love, root=repo)
    assert nxt is not None
    assert title_swap.title_slug(nxt) == "weatheringwithyou"
    assert title_swap.title_slug(nxt) != "frieren"


def test_youtube_watch_alts_do_not_block_romance_title_swap():
    """demo-v7-romance added Suzume YouTube URLs, which used to inject the
    slug 'watch' and exclude every other love title that also had a YouTube
    source (Your Name, Weathering With You)."""
    rows = json.loads((ROOT / "queue" / "demo.json").read_text())
    item = dict(next(i for i in rows if i.get("id") == "demo-v7-romance"))
    item["alt_source_urls"] = [
        "https://www.youtube.com/watch?v=Pbu0f1JgzVw",
        "https://www.youtube.com/watch?v=pEwoisyyvl0",
        "https://www.youtube.com/watch?v=aFNXuHyizz0",
    ]
    keys = title_swap.item_keys(item)
    assert "watch" not in keys
    nxt = title_swap.next_eligible_title(item, root=ROOT)
    assert nxt is not None, f"expected a love title, keys={keys}"
    assert title_swap.title_slug(nxt) in {
        "weatheringwithyou", "yourname", "apothecarydiaries", "apothecarydiaries2",
    }
    assert title_swap.item_format(nxt) == "love"
