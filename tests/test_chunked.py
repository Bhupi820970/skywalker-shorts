"""Chunked 1080p segment → concat → music mux → 4K path (no pipeline import)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import chunked


def test_is_heavy_long_source_or_previous_cancel():
    assert not chunked.is_heavy(duration_s=40, width=1920, height=1080, bitrate=3_000_000)
    assert chunked.is_heavy(duration_s=120)
    assert chunked.is_heavy(duration_s=60, bitrate=10_000_000)
    assert chunked.is_heavy(duration_s=50, width=3840, height=2160)
    assert chunked.is_heavy(last_error="The runner has received a shutdown signal")
    assert chunked.is_heavy(last_error="render job ended with no result JSON (timed out or cancelled mid-render)")
    assert chunked.is_heavy(item={"chunked": True, "id": "demo-v6-arcane"})
    assert not chunked.is_heavy(last_error="logo flash in shot 3")


def test_segment_count_is_four_or_five_for_a_short():
    assert chunked.segment_count(26) == 5
    assert chunked.segment_count(22) == 5
    assert chunked.segment_count(16) == 4
    assert chunked.segment_count(6) == 2
    bounds = chunked.segment_bounds(26, 5)
    assert len(bounds) == 5
    assert bounds[0][0] == 0
    assert bounds[-1][1] == pytest.approx(26)
    # contiguous
    for i in range(4):
        assert bounds[i][1] == pytest.approx(bounds[i + 1][0])


def test_split_timeline_keeps_shots_whole():
    shots = [
        {"t0": 0, "t1": 5, "scene": 1},
        {"t0": 5, "t1": 11, "scene": 2},
        {"t0": 11, "t1": 18, "scene": 3},
        {"t0": 18, "t1": 26, "scene": 4},
    ]
    bins = chunked.split_timeline(shots, n=5, total=26)
    assert 4 <= len(bins) <= 5
    flat = [s["scene"] for b in bins for s in b]
    assert flat == [1, 2, 3, 4]
    # no shot appears in two bins
    ids = [id(s) for b in bins for s in b]
    assert len(ids) == len(set(ids))


def test_stab_windows_cap_long_mezzanine():
    wins = chunked.stab_windows(180)
    assert 4 <= len(wins) <= 5
    assert wins[0][0] == 0
    assert wins[-1][1] == pytest.approx(180)
    assert chunked.should_chunk_stabilize(180)
    assert not chunked.should_chunk_stabilize(30) or chunked.chunked_active()


def test_ffmpeg_thread_args_are_capped():
    old = os.environ.get("SKY_FFMPEG_THREADS")
    try:
        os.environ.pop("SKY_FFMPEG_THREADS", None)
        assert chunked.ffmpeg_thread_args() == ["-threads", "2", "-filter_threads", "2"]
        os.environ["SKY_FFMPEG_THREADS"] = "8"
        assert chunked.ffmpeg_threads() == 4
        os.environ["SKY_FFMPEG_THREADS"] = "1"
        assert chunked.ffmpeg_threads() == 1
    finally:
        if old is None:
            os.environ.pop("SKY_FFMPEG_THREADS", None)
        else:
            os.environ["SKY_FFMPEG_THREADS"] = old


def test_activate_for_source_auto_vs_off(tmp_path):
    old = {k: os.environ.get(k) for k in ("SKY_CHUNKED", "SKY_CHUNKED_ACTIVE")}
    try:
        os.environ["SKY_CHUNKED"] = "off"
        os.environ.pop("SKY_CHUNKED_ACTIVE", None)
        assert chunked.activate_for_source(None, item={"chunked": True}) is False
        assert chunked.chunked_active() is False

        os.environ["SKY_CHUNKED"] = "on"
        assert chunked.activate_for_source(None) is True
        assert chunked.chunked_active() is True

        os.environ["SKY_CHUNKED"] = "auto"
        os.environ.pop("SKY_CHUNKED_ACTIVE", None)
        assert chunked.activate_for_source(None, item={"chunked": True}) is True
        assert os.environ["SKY_CHUNKED_ACTIVE"] == "1"

        os.environ["SKY_CHUNKED"] = "auto"
        repo = tmp_path
        (repo / "results").mkdir()
        (repo / "results" / "demo-v5-arcane.json").write_text(
            '{"id":"demo-v5-arcane","words":"ARCANE","last_error":'
            '"no result JSON; job cancelled or timed out","detail":"cancelled"}'
        )
        item = {"id": "demo-v6-arcane", "words": "ARCANE"}
        blob = chunked.last_error_blob(repo, item)
        assert "cancelled" in blob.lower()
        assert chunked.is_heavy(last_error=blob)
        assert chunked.activate_for_source(None, item=item, repo=repo) is True
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_write_concat_list_and_shift_ass(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    lst = tmp_path / "list.txt"
    chunked.write_concat_list([a, b], lst)
    text = lst.read_text()
    assert text.startswith("ffconcat version 1.0")
    assert "a.mp4" in text and "b.mp4" in text

    ass = tmp_path / "in.ass"
    ass.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:05.00,0:00:07.20,Default,hello\n"
        "Dialogue: 0,0:00:12.50,0:00:14.00,Default,world\n"
    )
    out = tmp_path / "out.ass"
    chunked.shift_ass_file(ass, out, -5.0)
    body = out.read_text()
    assert "0:00:00.00,0:00:02.20" in body
    assert "0:00:07.50,0:00:09.00" in body

    vf = "format=yuv420p,ass=/tmp/work/lowerthirds.ass:fontsdir=/tmp/fonts[v]"
    swapped = chunked.swap_ass_path(vf, out)
    assert "out.ass" in swapped
    assert "lowerthirds.ass" not in swapped


def test_run_with_retry_stops_after_two_retries():
    hits = {"n": 0}

    def boom():
        hits["n"] += 1
        raise RuntimeError(f"fail{hits['n']}")

    with pytest.raises(RuntimeError, match="fail3"):
        chunked.run_with_retry(boom, retries=2)
    assert hits["n"] == 3

    hits["n"] = 0

    def ok_on_second():
        hits["n"] += 1
        if hits["n"] < 2:
            raise RuntimeError("once")
        return "ok"

    assert chunked.run_with_retry(ok_on_second, retries=2) == "ok"
    assert hits["n"] == 2


def test_upscale_args_are_lanczos_4k():
    args = chunked.upscale_args()
    assert "scale=2160:3840:flags=lanczos" in args
    assert "libx264" in args


def test_mux_music_cmd_copies_video(tmp_path, monkeypatch):
    """mux_music must copy video (music already spans the full timeline)."""
    captured = {}

    class Fake:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"x" * 2000)
        return Fake()

    monkeypatch.setattr(chunked.subprocess, "run", fake_run)
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.wav"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")
    dest = tmp_path / "out.mp4"
    chunked.mux_music(video, audio, dest, duration=26)
    cmd = captured["cmd"]
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert str(video) in cmd and str(audio) in cmd
    assert "-t" in cmd and "26.000" in cmd


def test_bundle_cap_constant_matches_render():
    assert chunked.BUNDLE_CAP_S == 210.0


def test_refuse_single_pass_when_chunked_is_active():
    old = {k: os.environ.get(k) for k in ("SKY_CHUNKED", "SKY_CHUNKED_ACTIVE")}
    try:
        os.environ["SKY_CHUNKED"] = "off"
        os.environ["SKY_CHUNKED_ACTIVE"] = "0"
        assert chunked.refuse_single_pass() is False
        os.environ["SKY_CHUNKED"] = "auto"
        os.environ["SKY_CHUNKED_ACTIVE"] = "1"
        assert chunked.refuse_single_pass() is True
        os.environ["SKY_CHUNKED"] = "on"
        os.environ.pop("SKY_CHUNKED_ACTIVE", None)
        assert chunked.chunked_active() is True
        assert chunked.refuse_single_pass() is True
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
