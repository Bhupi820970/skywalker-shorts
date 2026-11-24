"""BiBi Nov 24 2026: Skywalker like the riyio reference edits - pre-drop silence + sub-bass riser, beat zoom
punches / shake / RGB split, slow push-in, ducked bed under dialogue, reference captions, dialogue -> drop
structure. Everything fail-safe."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sky_fx  # noqa: E402
import dialogue_caps as DC  # noqa: E402
import sky_lyrics as SL  # noqa: E402
import edit_mode  # noqa: E402

FF = shutil.which("ffmpeg") is not None
PIPE = (ROOT / "pipeline.py").read_text()


def _edl(lens, act1=0):
    out, t = [], 0.0
    for i, d in enumerate(lens):
        out.append({"i": i, "scene": i, "t0": t, "t1": t + d, "visible": d, **({"act": 1} if i < act1 else {})})
        t += d
    return out


# ------------------------------------------------------------------ 2 + 3: motion
def test_motion_plan_slow_zoom_and_tasteful_punches():
    edl = _edl([2.5, 3.0, 2.0, 0.6, 0.6, 0.6, 2.4, 0.5, 0.5, 0.5, 0.5, 3.0], act1=3)
    drop = 7.5
    plan = sky_fx.motion_plan(edl, drop, beats_out=[drop + 0.5 * k for k in range(30)])
    assert (0.0, 2.5) in plan["zooms"] and (2.5, 5.5) in plan["zooms"]     # every dialogue clip creeps in
    assert any(a >= drop for a, b in plan["zooms"])                         # long montage shot too
    assert (7.5, 8.1) in plan["pushes"] and all(b - a <= sky_fx.PUSH_MAX_S for a, b in plan["pushes"])
    assert all(b - a >= sky_fx.SLOW_ZOOM_MIN_S for a, b in plan["zooms"])
    p = plan["punches"]
    assert p[0] == drop and 3 <= len(p) <= sky_fx.PUNCH_MAX
    assert all(b - a >= sky_fx.PUNCH_MIN_GAP - 1e-6 for a, b in zip(p, p[1:]))   # never every frame
    assert all(t >= drop for t in p)                                          # only after the drop
    assert sky_fx.motion_plan(edl, None)["punches"] == []                     # no drop -> push-ins only
    assert sky_fx.motion_plan(edl, drop, punches=False)["punches"] == []


def test_zoom_curve_matches_ffmpeg_expression():
    plan = {"zooms": [(0.0, 2.0)], "punches": [3.0]}
    assert sky_fx.zoom_at(0.0, plan) == pytest.approx(1.0)
    assert sky_fx.zoom_at(1.0, plan) == pytest.approx(1.0 + sky_fx.SLOW_ZOOM_RATE * 2.0 / 2)   # ref ~1 %/s
    assert sky_fx.zoom_at(3.0 + sky_fx.PUNCH_S / 2, plan) == pytest.approx(1.0 + sky_fx.PUNCH_Z)
    assert sky_fx.zoom_at(3.0 + sky_fx.PUNCH_S + 0.01, plan) == pytest.approx(1.0)
    assert sky_fx.in_punch(3.1, plan) and not sky_fx.in_punch(2.5, plan)
    f = sky_fx.motion_filter(plan, 640, 360)
    assert "eval=frame" in f and "crop=640:360" in f and "rgbashift" in f and "clip(" in f
    assert sky_fx.PUNCH_Z == 0.08 and sky_fx.SLOW_ZOOM == 0.05 and sky_fx.SLOW_ZOOM_RATE == 0.012


def _frames(path, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    n = w * h
    return [np.frombuffer(raw[i * n:(i + 1) * n], np.uint8).reshape(h, w).astype(np.float32)
            for i in range(len(raw) // n)]


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_plate_fx_zooms_like_the_plan(tmp_path):
    clip = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    plan = {"zooms": [(0.0, 3.0)], "punches": [2.0]}
    out = sky_fx.apply_plate_fx(clip, None, 30, plan)
    assert out != clip
    a, b = _frames(clip, 320, 180), _frames(out, 320, 180)
    assert len(a) == len(b) == 90 and b[0].shape == (180, 320)               # same size, same length
    k = 63                                                                    # t=2.1: punch peak region
    z = sky_fx.zoom_at(k / 30, plan)
    from PIL import Image
    src = Image.fromarray(a[k].astype(np.uint8))
    cw, ch = 320 / z, 180 / z
    zoomed = np.array(src.crop((int((320 - cw) / 2), int((180 - ch) / 2), int((320 + cw) / 2),
                                int((180 + ch) / 2))).resize((320, 180)), np.float32)
    def corr(x, y):
        x, y = x - x.mean(), y - y.mean()
        return float((x * y).sum() / np.sqrt((x * x).sum() * (y * y).sum()))
    assert corr(b[k][20:-20, 20:-20], zoomed[20:-20, 20:-20]) > corr(b[k][20:-20, 20:-20], a[k][20:-20, 20:-20])


def test_plate_fx_fail_safe(tmp_path):
    missing = tmp_path / "nope.mp4"
    assert sky_fx.apply_plate_fx(missing, None, 30, {"zooms": [(0, 1)], "punches": []}) == missing
    assert sky_fx.apply_plate_fx(missing, 1.0, 30, None) == missing


# ------------------------------------------------------------------ 1: pre-drop silence + riser
def test_riser_rises():
    r = sky_fx.riser(1.6)
    q = len(r) // 4
    rms = [float(np.sqrt((r[i * q:(i + 1) * q] ** 2).mean())) for i in range(4)]
    assert rms[0] < rms[1] < rms[2] < rms[3] and abs(r[-1]).max() < 0.05


def test_pre_drop_window_never_cuts_a_word():
    assert sky_fx.pre_drop_window(10.0) == (9.8, 10.0)                       # split-second (ref1 ~0.1-0.2 s)
    assert sky_fx.pre_drop_window(10.0, speech_end=9.5) == (9.8, 10.0)
    assert sky_fx.pre_drop_window(10.0, speech_end=9.7) == (9.8, 10.0)
    assert sky_fx.pre_drop_window(10.0, speech_end=9.84) is None             # no room: keep the word
    assert sky_fx.pre_drop_window(0.6) is None


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_pre_drop_audio(tmp_path):
    a = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:d=6", "-af", "volume=0.3",
                    "-ac", "2", "-ar", "48000", str(a)], check=True)
    out = sky_fx.apply_pre_drop_audio(a, 4.0, tmp_path)
    assert out != a
    x = sky_fx._read_f32(out)[:, 0]
    sr = 48000
    rms = lambda t0, t1: float(np.sqrt((x[int(t0 * sr):int(t1 * sr)] ** 2).mean()))
    assert rms(3.81, 3.99) < 1e-4                                            # total silence right before the drop
    src = sky_fx._read_f32(a)[:, 0]
    assert rms(4.05, 4.5) == pytest.approx(float(np.sqrt((src[int(4.05 * sr):int(4.5 * sr)] ** 2).mean())), rel=0.05)
    assert rms(3.3, 3.78) > rms(1.0, 1.48)                                   # riser swells into the gap
    assert sky_fx.apply_pre_drop_audio(a, None, tmp_path) == a
    assert sky_fx.apply_pre_drop_audio(tmp_path / "missing.wav", 4.0, tmp_path) == tmp_path / "missing.wav"
    assert "[2:a]" not in sky_fx.sfx_mix_filter(4.0, whoosh=False)          # no whoosh inside the silence


# ------------------------------------------------------------------ 5: reference captions
WORDS = [{"t0": 0.2 + i * 0.3, "t1": 0.45 + i * 0.3, "word": w}
         for i, w in enumerate("Nice to meet you, Mike. You will never win this war, my king.".split())]
LAY = {"plate_x": 0, "plate_y": 656, "plate_w": 1080, "plate_h": 608}


def test_caption_groups_and_meaning_colours():
    g = DC.group_words(WORDS, 10.0)
    assert all(1 <= len(x) <= DC.MAX_WORDS <= 5 for x in g)
    assert [w["word"] for w in g[0]] == ["Nice", "to", "meet", "you,"]
    assert DC.accent_for("kill", 0) == DC.RED and DC.accent_for("love", 0) == DC.PINK
    assert DC.accent_for("king", 0) == DC.GOLD and DC.accent_for("winter", 0) == DC.BLUE
    st = DC.style_group(g[-1], 3, is_last=True)
    assert st[-1]["punch"] and st[-1]["drama"] and st[-1]["size"] == DC.SIZE_DRAMA > DC.SIZE_TALK
    assert len({it["size"] for it in st}) == 1                               # one size per line (refs)
    assert all(it["col"] == DC.WHITE for it in st if not it["key"])


def test_caption_placement_under_chin_never_on_face():
    face = (452, 719, 287, 417)
    (x, y, w, h), zone = DC.place(400, 90, LAY, [face])
    assert zone == "chin" and y >= face[1] + face[3] and abs(x + w / 2 - 540) < 2    # centred like the refs
    assert y + h <= LAY["plate_y"] + LAY["plate_h"]
    big = (300, 700, 480, 560)                                                # face fills the picture
    (x, y, w, h), zone = DC.place(400, 90, LAY, [big])
    assert DC._overlap((x, y, w, h), big) <= DC._overlap((340, 1100, 400, 90), big)
    (x, y, w, h), zone = DC.place(400, 90, LAY, [])
    assert zone == "mid" and abs(x + w / 2 - 540) < 2


def test_build_writes_reference_ass(tmp_path):
    res = DC.build(WORDS, 6.0, LAY, lambda t: [(452, 700, 200, 260)], tmp_path / "lowerthirds.ass")
    txt = (tmp_path / "lowerthirds.ass").read_text()
    assert "Montserrat Bold" in txt and "Cinzel Bold" in txt and r"\fscx120\fscy120\t(0,200,\fscx100\fscy100)" in txt
    assert txt.count("Dialogue:") == 2 * len(res["boxes"]) and len(res["boxes"]) >= 3   # glow layer + crisp layer
    assert all(b["t1"] <= 6.0 for b in res["boxes"]) and not DC.qa(res["boxes"], LAY)
    assert "KING" in res["text"]
    bad = [{"box": [1000, 700, 300, 80], "text": "x", "faces": []}]
    assert DC.qa(bad, LAY)


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_ass_renders_with_playfair(tmp_path):
    res = DC.build(WORDS[:4], 3.0, LAY, None, tmp_path / "c.ass")
    x, y, w, h = res["boxes"][0]["box"]
    png = tmp_path / "f.png"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=2",
                        "-vf", f"ass={tmp_path / 'c.ass'}:fontsdir={ROOT / 'fonts'}", "-ss", "1.4",
                        "-frames:v", "1", str(png)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("ffmpeg without libass")
    from PIL import Image
    im = np.array(Image.open(png).convert("L"), np.float32)
    assert im[y:y + h, x:x + w].max() > 200 and im[:y - 40].max() < 10       # text only inside its box


def test_lyrics_guard_ported():
    hanging = [{"word": w, "s": i * 0.5, "e": i * 0.5 + 0.4}
               for i, w in enumerate("And if I ain't not hanging tree. Are you, are you coming".split())]
    assert SL.lyric_reasons(hanging, 0.0, check_quote=False)
    spoken = [dict(w, s=w["t0"], e=w["t1"]) for w in WORDS]
    assert SL.lyric_reasons(spoken, 0.0, check_quote=False) == []
    kept, dropped = SL.filter_words(spoken + [{"word": "after", "s": 9.0, "e": 9.2}], 6.0, 0.0, None,
                                    {"title": "Never Win"})
    assert "never" not in SL.words_text(kept).lower() and any("outside" in d for d in dropped)


# ------------------------------------------------------------------ 4: ducked bed under dialogue
def test_dialogue_drop_mix(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    import vocal_mix as VM
    sr = 44100
    monkeypatch.setattr(VM, "separate_vocals", lambda a, b: False)
    t = np.arange(int(8 * sr)) / sr
    voice = (0.3 * np.sin(2 * np.pi * 220 * t) * ((t % 2.0) < 1.2)).astype(np.float32)   # talking bursts
    voice[int(4.0 * sr):] = 0
    VM._write(tmp_path / "dial.wav", np.stack([voice, voice], 1))
    rng = np.random.default_rng(0)
    song = (0.25 * rng.standard_normal(int(30 * sr))).astype(np.float32)
    VM._write(tmp_path / "song.wav", np.stack([song, song], 1))
    speech = [(k * 2.0, k * 2.0 + 1.2) for k in range(2)]
    res = VM.mix_dialogue_drop(tmp_path / "dial.wav", tmp_path / "song.wav", tmp_path / "mix.wav", 8.0, 4.0, 12.0,
                               speech, tmp_path)
    assert res["mode"] == "dialogue_drop" and res["voice_over_bed_db"] == pytest.approx(VM.DLG_VOICE_OVER_BED_DB, abs=2.5)
    assert res["voice_over_bed_db"] >= VM.DLG_MIN_SMR_DB
    assert res["voice_in_act2_dbfs"] < -50 and res["act2_song_dbfs"] > -30
    assert res["bed_under_voice_dbfs"] > -60                                 # the bed is there, just quiet
    assert not VM.check_errors(res, True)
    assert VM.check_errors(dict(res, voice_over_bed_db=6), True)
    assert json.loads((tmp_path / "audio_check.json").read_text())["mode"] == "dialogue_drop"


# ------------------------------------------------------------------ 6: structure / opt-in / QA wiring
def test_dialogue_edit_is_opt_in():
    assert edit_mode.dialogue_edit_wanted({"dialogue_edit": True}) is True
    assert edit_mode.dialogue_edit_wanted({"dialogue_edit": "true"}) is True
    assert edit_mode.dialogue_edit_wanted({}) is False
    assert edit_mode.dialogue_edit_wanted({"dialogue_edit": True}, env="0") is False
    assert edit_mode.dialogue_edit_wanted({}, env="1") is True
    assert edit_mode.choose_edit_mode("x", "two_act") == "music_only"        # live items stay song-only


def test_qc_gate_and_auto_qc_know_dialogue_edits():
    import gate
    import sky_ai
    assert sky_ai.qc_audio_mode("dialogue_drop", "", "action") == "dialogue_drop"
    assert sky_ai.qc_audio_mode("", "music_only", "action") == "music_only"
    assert gate.MAX_DUR == 32.6 and 45 < gate.DIALOGUE_MAX_DUR < 60
    src = (ROOT / "sky_ai.py").read_text()
    assert "captions_are_lyrics" in src and "song lyrics" in src
    assert 'get("dialogue_captions")' in (ROOT / "auto_qc.py").read_text()


def test_pipeline_wiring():
    for s in ("sky_fx.apply_plate_fx(silent, DROP_T, FPS, mplan", "sky_fx.apply_pre_drop_audio(",
              "whoosh=not pre_drop", "_mix_dialogue_drop(music, dur, src, edl)", "_reference_captions(edl, tr, lay",
              "_set_two_act_drop(edl)", "_sfx.zoom_at(t_out, mplan)", "_sfx.in_punch(", "DLG_ACT1_FRAC",
              'layout["edit_mode"] = "dialogue_drop"', "MAX_TOTAL_DUR = DLG_MAX_TOTAL if DIALOGUE_EDIT else 32.0"):
        assert s in PIPE, s
    assert "DLG_ACT1_FRAC, DLG_ACT1_MAX_FRAC, DLG_ACT1_MIN_S = 0.68, 0.85, 10.0" in PIPE
