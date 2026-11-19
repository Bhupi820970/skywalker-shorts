"""Same-topic retries, required Gemini QC, curated songs, and stock audit."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import backup_stock
import gate
import gemini
import music_lib
import sourcing


VINLAND = {
    "id": "demo-clips-vinlandsagathorfinn",
    "words": "VINLAND SAGA",
    "title": "Why Thorfinn refused to fight back in Vinland Saga",
    "fallback_asset": "vinlandsagathorfinn_scene1_h264.mp4",
    "fallback_mp4": [
        "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/vinlandsagathorfinn_scene1_h264.mp4"
    ],
}


def test_retry_sources_must_be_same_topic():
    assert sourcing.source_matches_topic(VINLAND, "vinlandsagathorfinn_scene1_h264.mp4")
    assert sourcing.source_matches_topic(VINLAND, "vinlandsagathorfinn_trailer_h264.mp4")
    # The bug: retry swapped in Blue Lock footage but kept the Vinland title.
    assert not sourcing.source_matches_topic(VINLAND, "bluelockisagi_scene1_h264.mp4")
    assert not sourcing.source_matches_topic(
        VINLAND,
        "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/bluelockisagi_scene1_h264.mp4",
    )
    assets = [
        "vinlandsagathorfinn_scene1_h264.mp4",
        "bluelockisagi_scene1_h264.mp4",
        "jjkgojovstoji_scene1_h264.mp4",
        "vinlandsagathorfinn_scene2_h264.mp4",
    ]
    same = [a for a in assets if sourcing.source_matches_topic(VINLAND, a)]
    assert same == ["vinlandsagathorfinn_scene1_h264.mp4", "vinlandsagathorfinn_scene2_h264.mp4"]


def test_qc_verdict_skipped_is_not_pass():
    assert gate.qc_verdict(None) == "skipped"
    assert gate.qc_verdict({"scores": {}}) == "skipped"
    assert gate.qc_verdict({"pass": True, "scores": {"hook": 9}}) == "pass"
    assert gate.qc_verdict({"pass": False, "reasons": ["crop"]}) == "fail"


def test_qc_skipped_result_is_not_ready():
    r = {
        "id": "stock-vinland-01",
        "status": "done",
        "asset_url": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/backup-stock/stock-vinland-01.mp4",
        "title": "Why Thorfinn refused to fight back",
        "source": "gh:https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/vinlandsagathorfinn_scene1_h264.mp4",
        "gate": {"ok": True, "gemini_qc": "skipped (unavailable/budget) - heuristic gate only"},
        "qc_status": "skipped",
    }
    assert backup_stock.qc_was_skipped(r)
    assert not backup_stock.is_ready_result(r)


def test_qc_retry_rotates_to_next_gemini_key(tmp_path, monkeypatch):
    budget = tmp_path / "budget.json"
    monkeypatch.setenv("SKY_GEMINI_BUDGET_FILE", str(budget))
    monkeypatch.setenv("GEMINI_API_KEY", "k-one")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k-two")
    monkeypatch.setenv("GEMINI_API_KEY_3", "k-three")
    monkeypatch.delenv("GEMINI_API_KEY_4", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_5", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_6", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_7", raising=False)
    names = [n for n, _ in gemini._ordered_keys()]
    assert names[0] == "GEMINI_API_KEY"
    gemini.allow_qc_retry(after_key="key1")
    assert gemini._budget().get("qc_retry") == 1
    rotated = [n for n, _ in gemini._ordered_keys()]
    assert rotated[0] == "GEMINI_API_KEY_2"
    assert rotated[-1] == "GEMINI_API_KEY"


def test_pick_uses_curated_phonk_not_generic_cc(tmp_path):
    libdir = tmp_path / "music" / "lib"
    libdir.mkdir(parents=True)
    tracks = [
        {"file": "sw_closeeyes.mp3", "title": "Close Eyes", "artist": "DVRST",
         "genre": "drift phonk", "tags": ["phonk", "drift", "edit", "trending"],
         "safety": "claim_risk", "credit": "Song: DVRST - Close Eyes"},
        {"file": "sw_moonwalkleaninmeaninphon.mp3",
         "title": "Moonwalk - Leanin' & Meanin' (Phonk Hip-Hop Beat)",
         "artist": "Moonwalk", "genre": "phonk", "tags": ["phonk", "edit", "trending"],
         "safety": "safe", "license": "http://creativecommons.org/licenses/by/3.0/",
         "credit": "Song: Moonwalk - Leanin' & Meanin' (Phonk Hip-Hop Beat) (http://creativecommons.org/licenses/by/3.0/)",
         "source": "archive.org", "source_id": "jamendo-525526"},
    ]
    (tmp_path / "music" / "library.json").write_text(json.dumps({"dir": "music/lib", "tracks": tracks}))
    for t in tracks:
        (libdir / t["file"]).write_bytes(b"x" * 100)
    assert music_lib.is_generic_cc_beat(tracks[1])
    assert music_lib.is_curated_edit_song(tracks[0])
    assert not music_lib.is_curated_edit_song(tracks[1])
    cat = music_lib.catalog(tmp_path)
    assert [t["file"] for t in cat] == ["sw_closeeyes.mp3"]
    picked = music_lib.pick(tmp_path, "anime", seed="demo")
    assert picked["file"] == "sw_closeeyes.mp3"


def test_generic_cc_allowed_only_when_curated_library_empty(tmp_path):
    libdir = tmp_path / "music" / "lib"
    libdir.mkdir(parents=True)
    tracks = [{
        "file": "sw_moonwalkleaninmeaninphon.mp3",
        "title": "Moonwalk - Leanin' & Meanin' (Phonk Hip-Hop Beat)",
        "artist": "Moonwalk", "genre": "phonk",
        "license": "http://creativecommons.org/licenses/by/3.0/",
        "credit": "Song: Moonwalk (http://creativecommons.org/licenses/by/3.0/)",
        "source_id": "jamendo-525526",
    }]
    (tmp_path / "music" / "library.json").write_text(json.dumps({"dir": "music/lib", "tracks": tracks}))
    (libdir / tracks[0]["file"]).write_bytes(b"x" * 100)
    picked = music_lib.pick(tmp_path, "anime", seed="demo")
    assert picked["file"] == "sw_moonwalkleaninmeaninphon.mp3"


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def test_audit_marks_mismatch_and_qc_skipped_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    monkeypatch.setattr(backup_stock, "REPO", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "results").mkdir()
    good = {
        "id": "stock-dune2-02", "status": "done",
        "asset_url": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/backup-stock/stock-dune2-02.mp4",
        "title": "Paul Atreides enters the battlefield #dune",
        "source": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/dune2_scene1_h264.mp4",
        "gate": {"ok": True, "gemini_qc": {"pass": True, "scores": {"hook": 8}}},
        "qc_status": "pass",
    }
    mismatch = {
        "id": "stock-vinlandsagathorfinn-01", "status": "done",
        "asset_url": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/backup-stock/stock-vinlandsagathorfinn-01.mp4",
        "title": "Why Thorfinn refused to fight back in Vinland Saga",
        "source": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/bluelockisagi_scene1_h264.mp4",
        "gate": {"ok": True, "gemini_qc": {"pass": True, "scores": {"hook": 8}}},
        "qc_status": "pass",
    }
    skipped = {
        "id": "stock-jjkgojovstoji-01", "status": "done",
        "asset_url": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/backup-stock/stock-jjkgojovstoji-01.mp4",
        "title": "Why Toji could not break Gojo",
        "source": "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/jjkgojovstoji_scene1_h264.mp4",
        "gate": {"ok": True, "gemini_qc": "skipped (unavailable/budget) - heuristic gate only"},
        "qc_status": "skipped",
    }
    items = []
    for r in (good, mismatch, skipped):
        _write(tmp_path / "results" / f"{r['id']}.json", r)
        slug = r["id"].split("-")[1] if not r["id"].startswith("stock-vinland") else "vinlandsagathorfinn"
        # parse_stock_id: stock-<slug>-<nn>
        slug = backup_stock.parse_stock_id(r["id"])[0]
        items.append({
            "id": r["id"], "topic_id": f"backup-{slug}", "slug": slug,
            "status": "ready", "asset_url": r["asset_url"],
        })
    _write(tmp_path / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2,
        "release": "backup-stock", "items": items,
    })
    # asset files are not deleted — we only have json + urls
    held = backup_stock.audit_ready_stock(tmp_path)
    held_ids = {h["id"]: h["reasons"] for h in held}
    assert "stock-vinlandsagathorfinn-01" in held_ids
    assert "topic_mismatch" in held_ids["stock-vinlandsagathorfinn-01"]
    assert "stock-jjkgojovstoji-01" in held_ids
    assert "qc_skipped" in held_ids["stock-jjkgojovstoji-01"]
    assert "stock-dune2-02" not in held_ids
    man = json.loads((tmp_path / "config" / "backup_stock.json").read_text())
    by = {e["id"]: e for e in man["items"]}
    assert by["stock-dune2-02"]["status"] == "ready"
    assert by["stock-vinlandsagathorfinn-01"]["status"] == "not_ready"
    assert by["stock-jjkgojovstoji-01"]["status"] == "not_ready"
    # videos are not deleted: result + asset_url remain
    for iid in ("stock-vinlandsagathorfinn-01", "stock-jjkgojovstoji-01", "stock-dune2-02"):
        rr = json.loads((tmp_path / "results" / f"{iid}.json").read_text())
        assert rr["status"] == "done"
        assert rr.get("asset_url")
