"""Love template, hook bans, GTA 6 launch gate, daily quotas."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import edit_format
import hook_text
import captions


def test_love_pacing_is_slower_than_action():
    p = edit_format.pacing_for("love")
    assert p["min_shot_len"] >= 2.5
    assert p["hero_shot_min"] >= 3.0
    assert p["max_flash_shots"] == 0
    assert p["target_dur_min"] >= 18
    assert edit_format.pacing_for("action") == {}


def test_love_caption_is_serif_center_not_lower_third():
    st = edit_format.caption_style("love")
    assert st["style"] == "cinematic_center"
    assert "Serif" in st["font_name"]
    assert st["keyword_highlight"] is False
    y = captions.caption_y(style="cinematic_center", plate_y=400, plate_h=900)
    # Lower-center of the plate (~68%), still above the Shorts UI floor.
    assert 400 < y < 1920 - 240
    assert y != captions.caption_y()


def test_reference_short_wording_is_banned():
    # Style was learned from public Shorts; exact lines must never be reused.
    assert edit_format.hook_uses_banned_phrase("It was love at first sight")
    assert edit_format.hook_uses_banned_phrase("Love, I would kill")
    assert not edit_format.hook_uses_banned_phrase("She already knew")
    errs = hook_text.check_item_hook_fields({
        "hook": "It was love at first sight",
        "title": "It was love at first sight | Bridgerton | #edit",
    })
    assert any("banned reference" in e for e in errs)


def test_gta6_not_launched_before_nov_19():
    before = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    after = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
    assert edit_format.gta6_has_launched(before) is False
    assert edit_format.gta6_has_launched(after) is True
    q0 = edit_format.daily_quotas(before)
    assert q0["gta6_min"] == 0 and q0["gta6_max"] == 0
    assert q0["love_min"] == 2 and q0["love_max"] == 3
    q1 = edit_format.daily_quotas(after)
    assert q1["gta6_min"] == 2 and q1["gta6_max"] == 3


def test_is_gta6_item():
    assert edit_format.is_gta6_item({"words": "GTA 6", "slug": "gta6"})
    assert edit_format.is_gta6_item({"title": "Grand Theft Auto VI official gameplay"})
    assert not edit_format.is_gta6_item({"words": "ARCANE", "pillar": "anime"})


def test_trending_score_boosts_matching_titles():
    assert edit_format.trending_score("BRIDGERTON official scene", titles=["Bridgerton", "Arcane"]) > 0
    assert edit_format.trending_score("Some Obscure Film", titles=["Bridgerton", "Arcane"]) == 0


def test_explicit_text_blocks_sexual_not_cute_romance():
    assert edit_format.is_explicit_text("Official uncensored sex scene")
    assert edit_format.is_explicit_text("Ecchi fan service clip")
    assert edit_format.is_explicit_item({"title": "Nude making love scene", "hook": "Too far"})
    assert not edit_format.is_explicit_text("Your Name official romance scene")
    assert not edit_format.is_explicit_text("A cute almost-kiss on the train")
    assert not edit_format.is_explicit_item({"title": "She already knew | Bridgerton | #edit",
                                            "content_kind": "romance scene"})
