"""Metadata must describe the show whose clips are in the video (20261121-1330-yourname used JJK clips)."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backup_stock
import title_swap

REL = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/"
YOURNAME = {
    "id": "20261121-1330-yourname", "words": "YOUR NAME", "hook": "He forgot her name again",
    "title": "He forgot her name again | Your Name | #edit", "tags": ["yourname", "anime"],
    "description": "From official Your Name clips. #yourname",
    "sources": [REL + "yourname_scene1_h264.mp4"], "fallback_mp4": [REL + "yourname_scene1_h264.mp4"],
    "fallback_release": "sources", "fallback_asset": "yourname_scene1_h264.mp4",
}
JJK_SRC = "gh:" + REL + "jujutsukaisencullinggame_scene2_h264.mp4"


def test_other_show_clip_is_flagged():
    assert title_swap.footage_title_mismatch(YOURNAME, JJK_SRC)
    assert not title_swap.footage_title_mismatch(YOURNAME, "gh:" + REL + "yourname_scene1_h264.mp4")
    # page URLs carry no show slug: nothing to compare, never a false refusal
    assert not title_swap.footage_title_mismatch(YOURNAME, "https://www.youtube.com/watch?v=Ja1q9DnLI6M")


def test_qc_retry_restores_swapped_show_metadata():
    prev = {"swapped_from": "yourname", "swapped_to": "jujutsukaisencullinggame", "words": "JUJUTSU KAISEN",
            "title": "Why Higuruma cannot look away #jujutsukaisen", "hook": "He still remembers the silence",
            "description": "Higuruma and Yuji. #jujutsukaisen", "tags": ["jujutsukaisen"], "source": JJK_SRC}
    it = copy.deepcopy(YOURNAME)
    assert title_swap.restore_swap_from_result(it, prev)
    assert it["title"] == prev["title"] and it["words"] == "JUJUTSU KAISEN" and it["tags"] == ["jujutsukaisen"]
    assert it["id"] == YOURNAME["id"]  # slot/id kept
    assert not title_swap.footage_title_mismatch(it, JJK_SRC)
    # no swap recorded -> nothing to restore, and the mismatch stays flagged
    it2 = copy.deepcopy(YOURNAME)
    assert not title_swap.restore_swap_from_result(it2, {"source": JJK_SRC})
    assert title_swap.footage_title_mismatch(it2, JJK_SRC)


def test_publish_skips_mismatched_results():
    r = {"id": "20261121-1330-yourname", "status": "done", "asset_url": "x", "slot": "2026-11-21T13:30:00+05:30",
         "footage_mismatch": "footage/title mismatch"}
    assert backup_stock.must_not_publish(r)
    r.pop("footage_mismatch")
    assert not backup_stock.must_not_publish(r)
