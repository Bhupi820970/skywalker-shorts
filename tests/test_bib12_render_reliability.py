#!/usr/bin/env python3
"""BIB-12: demo-v14/v15 produced no video.

1. Logo-scan 429/503 storms ate the shared Gemini budget (81 HTTP on logo_scenes), so the
   scene pick / hook / ref-match plan / QC all said "budget exhausted".
2. A dialogue (act-1) line replayed frames auto_qc had punched out, so the same 8 text
   frames failed three re-slices in a row.
3. Demo items swapped from a trailer-only topic to random unproven footage (Bleach).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gemini  # noqa: E402
import title_swap  # noqa: E402

OK = ("ok", {"candidates": [{"content": {"parts": [{"text": '{"flagged": []}'}]}}]})


def _env(monkeypatch, tmp_path, n_keys=7):
    for name in gemini.KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    for i, name in enumerate(gemini.KEY_ENVS[:n_keys]):
        monkeypatch.setenv(name, f"dummy-{i}")
    monkeypatch.setenv("SKY_GEMINI", "1")
    monkeypatch.setenv("SKY_GEMINI_BUDGET_FILE", str(tmp_path / "b.json"))
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)


def test_logo_storm_does_not_starve_creative_or_qc_calls(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    state = {"storm": True}

    def fake(model, key, body, timeout):
        if state["storm"]:
            return ("http", 503, b"")
        return OK

    monkeypatch.setattr(gemini, "_post", fake)
    for _ in range(8):
        assert gemini.generate_json("x", purpose="logo_scenes") is None
    b = json.loads((tmp_path / "b.json").read_text())
    assert b.get("calls", 0) == 0 and b.get("http", 0) == 0  # main pool untouched
    assert b["logo_http"] <= 8 * 10                          # per-call cap holds
    state["storm"] = False
    b["cool_models"] = {}
    (tmp_path / "b.json").write_text(json.dumps(b))
    assert gemini.generate_json("x", purpose="scenes") == {"flagged": []}
    assert gemini.generate_json("x", purpose="hook") == {"flagged": []}
    assert gemini.generate_json("x", purpose="qc") == {"flagged": []}


def test_503_moves_to_next_model_not_every_key(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    seen = []

    def fake(model, key, body, timeout):
        seen.append((key, model))
        return ("http", 503, b"") if model == gemini.DEFAULT_MODELS[0] else OK

    monkeypatch.setattr(gemini, "_post", fake)
    assert gemini.generate_json("x", purpose="scenes") is not None
    assert seen == [("dummy-0", gemini.DEFAULT_MODELS[0]), ("dummy-0", gemini.DEFAULT_MODELS[1])]
    # The overloaded model stays cooled for the next call too (no re-hammering).
    seen.clear()
    assert gemini.generate_json("x", purpose="hook") is not None
    assert seen == [("dummy-0", gemini.DEFAULT_MODELS[1])]


def test_429_moves_to_next_key_on_the_same_model(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    seen = []

    def fake(model, key, body, timeout):
        seen.append((key, model))
        return ("http", 429, b"") if key in ("dummy-0", "dummy-1") else OK

    monkeypatch.setattr(gemini, "_post", fake)
    assert gemini.generate_json("x", purpose="scenes") is not None
    m0 = gemini.DEFAULT_MODELS[0]
    assert seen == [("dummy-0", m0), ("dummy-1", m0), ("dummy-2", m0)]
    blob = (tmp_path / "b.json").read_text()
    assert "dummy-" not in blob  # never the secret


def test_one_call_is_capped_and_waits_out_cooldowns(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, n_keys=2)
    monkeypatch.setenv("GEMINI_CALL_MAX_HTTP", "6")
    slept = []
    monkeypatch.setattr(gemini.time, "sleep", lambda s: slept.append(s))
    n = {"c": 0}

    def fake(model, key, body, timeout):
        n["c"] += 1
        return ("http", 429, b"")

    monkeypatch.setattr(gemini, "_post", fake)
    assert gemini.generate_json("x", purpose="scenes") is None
    assert n["c"] == 6
    assert any(s >= 10 for s in slept)  # real backoff once every key is rate-limited


def test_main_budget_cap_still_holds(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    (tmp_path / "b.json").write_text(json.dumps({"calls": 5, "http": 0}))
    monkeypatch.setattr(gemini, "_post", lambda *a: OK)
    assert gemini.generate_json("x", purpose="scenes") is None
    assert gemini.generate_json("x", purpose="logo_final") == {"flagged": []}  # safety scan still runs
    assert gemini.generate_json("x", purpose="qc") == {"flagged": []}


# ---------------------------------------------------------------- act-1 coverage

def _pl():
    import pytest
    pytest.importorskip("librosa")  # CI's light job has no audio stack
    import pipeline
    return pipeline


def test_act1_line_never_crosses_a_qc_punched_hole():
    pipeline = _pl()
    words = [{"word": w, "start": 30.0 + i * 0.5, "end": 30.4 + i * 0.5, "prob": 0.9}
             for i, w in enumerate("I had no idea it would be this rough.".split())]
    tr = {"words": words, "segments": []}
    whole = [{"id": 11, "start": 28.0, "end": 38.0, "dur": 10.0}]
    assert pipeline.act1_candidates(whole, tr), "clean scene: the line is usable"
    # auto_qc punched 32.3-34.6 (burned-in text) -> scene 11 + leftover 11101
    punched = [{"id": 11, "start": 28.0, "end": 32.3, "dur": 4.3},
               {"id": 11101, "start": 34.6, "end": 38.0, "dur": 3.4}]
    for c in pipeline.act1_candidates(punched, tr):
        assert c["a"] + c["vis"] <= 32.3 + 1e-6 or c["a"] >= 34.6 - 1e-6, c


def test_qc_blacklist_punches_the_scene_that_holds_the_frame():
    pipeline = _pl()
    scenes = [{"id": 1, "start": 0.0, "end": 4.0, "dur": 4.0},
              {"id": 2, "start": 4.0, "end": 9.0, "dur": 5.0}]
    # one act-1 item tagged with scene 1 that plays source 2.0-8.0 across the cut
    edl = [{"scene": 1, "t0": 0.0, "t1": 6.0, "src_start": 2.0, "visible": 6.0, "act": 1}]
    n = pipeline.apply_qc_blacklist(scenes, edl, [{"t": 4.0}])  # source 6.0 -> scene 2
    assert n == 1
    s2 = [s for s in scenes if s["id"] == 2 or s.get("parent_id") == 2]
    assert s2 and all(not (s["start"] < 6.0 < s["end"]) for s in s2 if not s.get("skip"))
    s1 = next(s for s in scenes if s["id"] == 1)
    assert (s1["start"], s1["end"]) == (0.0, 4.0)


# ---------------------------------------------------------------- demo swap

def _scene(slug, n=1):
    return f"https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/{slug}_scene{n}_h264.mp4"


def test_demo_swaps_only_to_proven_footage(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "queue").mkdir()
    (tmp_path / "results").mkdir()
    topics = [
        {"slug": "bleachtybw", "pillar": "anime", "words": "BLEACH", "hook": "Ichigo was born for this war",
         "title": "t | Bleach | #edit", "scene_assets": ["bleachtybw_scene1_h264.mp4", "bleachtybw_scene2_h264.mp4",
                                                         "bleachtybw_scene3_h264.mp4"], "content_kind": "best scene"},
        {"slug": "oppenheimer", "pillar": "hollywood", "words": "OPPENHEIMER", "hook": "Why he regrets his creation",
         "title": "t | Oppenheimer | #edit", "scene_assets": ["oppenheimer_scene1_h264.mp4"], "content_kind": "best scene"},
    ]
    (tmp_path / "config" / "topic_catalog.json").write_text(json.dumps({"items": topics}))
    (tmp_path / "config" / "backup_pool.json").write_text(json.dumps({"items": []}))
    (tmp_path / "config" / "source_catalog.json").write_text(json.dumps({"topics": {}}))
    (tmp_path / "queue" / "demo.json").write_text("[]")
    at = (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M IST")
    (tmp_path / "results" / "20991231-2230-oppenheimer.json").write_text(json.dumps(
        {"id": "20991231-2230-oppenheimer", "status": "done", "asset_url": "x", "at": at}))
    (tmp_path / "results" / "20991231-0900-bleachtybw.json").write_text(json.dumps(
        {"id": "20991231-0900-bleachtybw", "status": "failed", "at": at,
         "detail": "only 14.9s of unique clean shots (need ≥18s)"}))
    item = {"id": "demo-x", "demo": True, "no_publish": True, "pillar": "movie", "words": "SUPERMAN",
            "hook": "He never stopped saving them", "title": "t | Superman | #edit",
            "sources": ["https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/superman_trailer_h264.mp4"]}
    nxt = title_swap.next_eligible_title(item, root=tmp_path)
    assert nxt and title_swap.title_slug(nxt) == "oppenheimer"
