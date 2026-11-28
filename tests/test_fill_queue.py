"""fill_queue should not clone a hook or slug already in the lookahead window."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.fill_queue as fq


def test_used_in_days_counts_id_slug_not_tags(tmp_path, monkeypatch):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    monkeypatch.setattr(fq, "QUEUE", qdir)
    day = "2026-11-24"
    (qdir / (day + ".json")).write_text(json.dumps([
        {
            "id": "20261124-1130-suzume",
            "hook": "She keeps closing doors alone",
            "title": "She keeps closing doors alone | Suzume | #edit",
            "tags": ["dandadan", "anime", "suzume"],
            "status": "queued",
        }
    ]))
    slugs, hooks = fq._used_in_days([day])
    assert slugs == {"suzume"}
    assert "dandadan" not in slugs
    assert "she keeps closing doors alone" in hooks


def test_fill_day_does_not_clone_window_slug(tmp_path, monkeypatch):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    monkeypatch.setattr(fq, "QUEUE", qdir)
    day = "2026-11-25"
    (qdir / "2026-11-24.json").write_text(json.dumps([
        {
            "id": "20261124-1830-jjk0",
            "hook": "Yuta only fights for her",
            "title": "Yuta only fights for her | Jujutsu Kaisen 0 | #edit",
            "status": "queued",
        }
    ]))
    (qdir / (day + ".json")).write_text("[]\n")
    catalog = [
        {
            "slug": "jjk0",
            "pillar": "romance",
            "words": "JUJUTSU KAISEN 0",
            "hook": "Yuta only fights for her",
            "title": "Yuta only fights for her | Jujutsu Kaisen 0 | #edit",
            "description": "From official clips. Edit by Skywalker.",
            "tags": ["jjk0"],
            "source_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
            "scene_assets": ["jjk0_scene2_h264.mp4"],
            "content_kind": "romance scene",
        }
    ]
    have = {"jjk0_scene2_h264.mp4"}
    used, hooks = fq._used_in_days(["2026-11-24", day])
    added = fq.fill_day(day, catalog, used, have, hooks)
    assert added == 0
    assert json.loads((qdir / (day + ".json")).read_text()) == []


def test_item_slug_parses_compound_ids():
    assert fq._item_slug({"id": "20261125-1600-cyberpunkedgerunners"}) == "cyberpunkedgerunners"
    assert fq._item_slug({"id": "bad"}) == ""


def test_posted_hooks_reads_history_and_stats(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "stats").mkdir()
    (tmp_path / "config" / "posted_history.json").write_text(json.dumps({
        "items": [
            {"id": "20261121-2230-arcane", "title": "Jinx finally loses her mind #arcane #jinx",
             "hook": "Jinx finally loses her mind"}
        ]
    }))
    (tmp_path / "stats" / "latest.json").write_text(json.dumps({
        "videos": {
            "abc": {"title": "Thorfinn took every punch | Vinland Saga | #edit",
                    "hook": "Thorfinn took every punch"}
        }
    }))
    monkeypatch.setattr(fq, "REPO", tmp_path)
    hooks = fq._posted_hooks()
    assert "jinx finally loses her mind" in hooks
    assert "thorfinn took every punch" in hooks
    assert "thorfinn took every punch | vinland saga |" in hooks


def test_fill_day_skips_hooks_already_on_channel(tmp_path, monkeypatch):
    qdir = tmp_path / "queue"
    qdir.mkdir()
    monkeypatch.setattr(fq, "QUEUE", qdir)
    monkeypatch.setattr(fq, "_posted_hooks", lambda root=None: {"jinx finally loses her mind"})
    day = "2026-11-30"
    (qdir / (day + ".json")).write_text("[]" + chr(10))
    catalog = [
        {
            "slug": "arcane",
            "pillar": "anime",
            "words": "ARCANE",
            "hook": "Jinx finally loses her mind",
            "title": "Jinx finally loses her mind | Arcane | #edit",
            "description": "From official clips. Edit by Skywalker.",
            "tags": ["arcane"],
            "source_url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
            "scene_assets": ["arcane_scene1_h264.mp4"],
            "content_kind": "best scene",
        }
    ]
    have = {"arcane_scene1_h264.mp4"}
    added = fq.fill_day(day, catalog, set(), have, set())
    assert added == 0
    assert json.loads((qdir / (day + ".json")).read_text()) == []
