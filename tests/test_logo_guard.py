"""No-logo guard: edge trims, dense scan rejection, QC judge logo wording, gate blocking."""
import subprocess
from pathlib import Path

import pytest

import logo_guard as lg
import sky_ai


def test_edge_guard_skips_source_tail_end_card():
    # v9 Dandadan: Netflix SUBSCRIBE end card filled the last ~14 s of a 98 s source.
    scenes = [{"start": 0.0, "end": 2.0, "dur": 2.0}, {"start": 10.0, "end": 14.0, "dur": 4.0},
              {"start": 82.0, "end": 86.0, "dur": 4.0}, {"start": 88.3, "end": 89.8, "dur": 1.5}]
    lg.apply_edge_guard(scenes, 98.29)
    assert scenes[0]["skip"] and scenes[0]["reason"] == "edge_card_guard"
    assert not scenes[1].get("skip")
    assert not scenes[2].get("skip") and scenes[2]["end"] <= 98.29 - 15.0 + 1e-6
    assert scenes[3]["skip"]


def test_sample_times_dense():
    ts = lg.sample_times(10.0, 12.0, 2.0)
    assert ts[0] >= 10.0 and ts[-1] <= 12.0 and len(ts) >= 4


@pytest.fixture
def vid(tmp_path):
    p = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                    "-t", "6", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def test_scan_scenes_skips_flagged_scene(vid, tmp_path):
    scenes = [{"start": 0.0, "end": 2.5}, {"start": 3.0, "end": 5.5}]
    ask = lambda prompt, imgs: {"flagged": [{"i": 0, "what": "Netflix N"}]}
    assert "Netflix" in lg.PROMPT.format(brands=lg.BRANDS)
    r = lg.scan_scenes(vid, scenes, tmp_path, ask=ask)
    assert r["ok"] is False
    assert scenes[0]["skip"] and scenes[0]["reason"] == "gemini_logo"
    assert not scenes[1].get("skip")


def test_scan_without_verdict_is_not_clean(vid, tmp_path):
    r = lg.scan_video(vid, tmp_path, ask=lambda p, i: None)
    assert r["ok"] is None
    r = lg.scan_video(vid, tmp_path, ask=lambda p, i: {"flagged": []})
    assert r["ok"] is True and r["frames"] >= 12


def test_qc_reason_mentioning_netflix_is_a_logo():
    assert sky_ai.logo_mentioned(["The only minor issue is the presence of a 'Netflix Subscribe' watermark"])
    assert not sky_ai.logo_mentioned(["Clean footage with no logo or watermark."])


def test_gate_blocks_on_logo_hit(vid, monkeypatch):
    import gate
    g = gate.run_gate(vid, qa_obj={}, checks={}, item={}, work=None, gemini_qc=None, out_res=1080,
                      logo_scan=lambda f: {"ok": False, "hits": {"final": ["0.50s Netflix N"]}})
    assert not g["ok"] and any(f.startswith("logo scan") for f in g["failures"])
    g = gate.run_gate(vid, qa_obj={}, checks={}, item={}, work=None, gemini_qc=None, out_res=1080,
                      logo_scan=lambda f: {"ok": None, "error": "no gemini verdict"})
    assert any(f.startswith("logo scan") for f in g["failures"])
