"""BiBi Nov 24 2026 "ek ek baariki dekh kr vasa kr": Skywalker tuned to the values measured on ref1 / ref2, Gemini
plans the dialogue edit (scenes, punch line, montage, caption accents / reveals), savage hook bar line, and ONE switch
(SKY_DIALOGUE_EDIT=1) for dialogue-first edits with a song-only fallback."""
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
import edit_mode  # noqa: E402
import sky_ai  # noqa: E402

FF = shutil.which("ffmpeg") is not None
PIPE = (ROOT / "pipeline.py").read_text()
LAY = {"plate_x": 0, "plate_y": 656, "plate_w": 1080, "plate_h": 608}


# ------------------------------------------------------------------ measured motion / flash / audio values
def test_measured_constants():
    assert sky_fx.SLOW_ZOOM_RATE == 0.012 and sky_fx.SLOW_ZOOM == 0.05          # refs creep 0.6-1.2 %/s
    assert 0.06 <= sky_fx.PUSH_A <= 0.10 and sky_fx.PUSH_TAU <= 0.08               # +8-10 % in the first ~0.15 s
    assert 0.06 <= sky_fx.PRE_A <= 0.10 and sky_fx.PRE_S == 0.10                   # zoom into the next cut
    assert sky_fx.SHAKE_FRAC * 1080 < sky_fx.PUSH_BASE * 1080 / 2                  # shake fits the push margin
    assert "rh=-1:bh=1" in sky_fx.PUNCH_RGB                                        # measured R-B offset < 0.6 px
    assert sky_fx.PRE_DROP_SILENCE == 0.2 and sky_fx.RISER_S == 1.2
    assert sky_fx.REF_FLASH_PEAK == 0.8


def test_montage_push_curve():
    edl = [{"t0": 0, "t1": 6, "scene": 0, "act": 1}, {"t0": 6, "t1": 6.45, "scene": 1},
           {"t0": 6.45, "t1": 6.9, "scene": 2}, {"t0": 6.9, "t1": 9.0, "scene": 3}]
    plan = sky_fx.motion_plan(edl, 6.0, [6.0, 6.45, 6.9])
    assert plan["pushes"] == [(6.0, 6.45), (6.45, 6.9)] and (6.9, 9.0) in plan["zooms"]
    z = [sky_fx.zoom_at(6.0 + k * 0.03, plan) for k in range(15)]
    assert all(b >= a - 1e-9 for a, b in zip(z, z[1:]))                       # pushes in through the shot
    assert z[5] - z[0] > 0.05                                                    # fast ease-out start
    assert sky_fx.zoom_at(6.449, plan) > 1.15 and sky_fx.zoom_at(6.451, plan) < 1.06   # resets on the cut
    assert sky_fx.zoom_at(3.0, plan) == pytest.approx(1 + 0.05 * 0.5)            # 6 s dialogue clip: capped 5 %
    assert sky_fx.in_punch(6.40, plan) and not sky_fx.in_punch(3.0, plan)
    f = sky_fx.motion_filter(plan, 640, 360)
    assert "tmix=frames=3" in f and "exp(" in f and f"white@{sky_fx.KICK_FLASH}" in f


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_push_and_ref_flash_render(tmp_path):
    clip = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    plan = {"zooms": [(0.0, 1.0)], "pushes": [(1.0, 1.45), (1.45, 2.0)], "punches": [1.0, 1.45]}
    out = sky_fx.apply_plate_fx(clip, 1.0, 30, plan, ref_flash=True)
    assert out != clip
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", "format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, 180, 320).astype(np.float32)
    assert len(fr) == 60
    lum = fr.mean(axis=(1, 2))
    assert lum[30] > lum[20] + 60                                                # white flash on the drop
    assert lum[27] > lum[20] + 5                                                 # builds up before it (ref1)
    assert lum[42] < lum[30] - 40                                                # gone ~0.35 s later


def test_ref_flash_shape():
    f = sky_fx.ref_flash_filter(2.0, 30)
    import re
    alphas = [float(a) for a in re.findall(r"white@([0-9.]+)", f)]
    assert max(alphas) == sky_fx.REF_FLASH_PEAK and "eq(n,60)" in f
    assert f.count("drawbox") <= int((sky_fx.REF_FLASH_IN + sky_fx.REF_FLASH_OUT) * 30) + 2
    assert "white@0.6" in sky_fx.drop_video_filter(2.0, 30)                     # song-only keeps the soft flash


def test_bed_level_solved_like_refs(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    import vocal_mix as VM
    sr = 44100
    monkeypatch.setattr(VM, "separate_vocals", lambda a, b: False)
    t = np.arange(int(8 * sr)) / sr
    voice = (0.3 * np.sin(2 * np.pi * 220 * t) * ((t % 2.0) < 1.2)).astype(np.float32)
    voice[int(4.0 * sr):] = 0
    VM._write(tmp_path / "dial.wav", np.stack([voice, voice], 1))
    for amp in (0.2, 0.6):                                                        # normal / loud song intro
        song = (amp * np.random.default_rng(1).standard_normal(int(30 * sr))).astype(np.float32)
        VM._write(tmp_path / "song.wav", np.stack([song, song], 1))
        res = VM.mix_dialogue_drop(tmp_path / "dial.wav", tmp_path / "song.wav", tmp_path / "mix.wav", 8.0, 4.0,
                                   12.0, [(0.0, 1.2), (2.0, 3.2)], tmp_path)
        assert res["voice_over_bed_db"] == pytest.approx(12.0, abs=2.5), res
        assert not VM.check_errors(res, True)
    assert VM.DLG_MIN_SMR_DB == 9.0


# ------------------------------------------------------------------ captions measured on the refs
def test_caption_fonts_are_static_bold():
    from PIL import ImageFont
    for f in (DC.FONT_SANS, DC.FONT_SERIF):
        assert f.exists() and (ROOT / "fonts" / f"OFL-{f.stem.split('-')[0]}.txt").exists()
        ImageFont.truetype(str(f), 40)
    assert DC.SANS_NAME == "Montserrat Bold" and DC.SERIF_NAME == "Cinzel Bold"
    assert 37 <= DC.SIZE_TALK <= 46 and 55 <= DC.SIZE_DRAMA <= 66               # ref cap height 2.4-2.9 % / 4.3 %


def test_typewriter_and_outline_lines(tmp_path):
    words = [{"t0": 0.2 + i * 0.3, "t1": 0.45 + i * 0.3, "word": w, "src_start": 10 + i * 0.3}
             for i, w in enumerate("You have a lot of money. Winter came.".split())]
    res = DC.build(words, 4.0, LAY, None, tmp_path / "c.ass")
    txt = (tmp_path / "c.ass").read_text()
    serif = [ln for ln in txt.splitlines() if ",RefSerif," in ln]
    sans = [ln for ln in txt.splitlines() if ",RefSans," in ln]
    assert serif and sans
    assert max(ln.count(r"\t(") for ln in serif) >= len("WINTER")             # typed letter by letter
    assert r"\1a&HFF&\bord2" in txt                                              # outline-only impact word
    assert all(abs(b["box"][0] + b["box"][2] / 2 - 540) < 2 for b in res["boxes"])   # centred like the refs


# ------------------------------------------------------------------ Gemini dialogue plan (validated)
LINES = [{"id": 0, "start": 10.0, "end": 12.0, "text": "You have a lot of money."},
         {"id": 1, "start": 13.0, "end": 15.0, "text": "Winter came for House Frey."},
         {"id": 2, "start": 16.0, "end": 17.0, "text": "Okay then."}]


def test_parse_dialogue_plan_validates():
    out = {"punch_line": 1, "captions": [
        {"line": 0, "reveal": "pop", "accent": [{"word": "money", "color": "green"},
                                                {"word": "dragon", "color": "red"},      # not in the line
                                                {"word": "lot", "color": "purple"}]},    # unknown colour
        {"line": "L1", "reveal": "typewriter", "accent": [{"word": "Winter", "color": "blue"}]},
        {"line": 9, "reveal": "pop"},                                                     # unknown id
        {"line": 2, "reveal": "sparkle"}]}                                                # nothing valid
    arr = sky_ai.parse_dialogue_plan(out, LINES, {"act1": [1, 0]})
    assert arr["act1"] == [0, 1] and arr["punch_line"] == 1                    # punch line moved last
    plan = {p["line"]: p for p in arr["caption_plan"]}
    assert set(plan) == {0, 1}
    assert plan[0]["accent"] == [{"word": "money", "color": "green"}] and plan[1]["reveal"] == "typewriter"
    assert plan[1]["src"] == [13.0, 15.0]
    assert sky_ai.parse_dialogue_plan({"captions": "junk"}, LINES, {"act1": []}) == {"act1": []}


def test_caption_plan_drives_style_with_rule_fallback():
    words = [{"t0": i * 0.3, "t1": i * 0.3 + 0.25, "word": w, "src_start": s}
             for i, (w, s) in enumerate([("You", 10.1), ("have", 10.4), ("money.", 10.8),
                                         ("Winter", 13.1), ("came.", 13.5), ("Kill", 20.0), ("him.", 20.3)])]
    plan = [{"line": 0, "src": [10.0, 12.0], "reveal": "pop", "accent": [{"word": "money", "color": "gold"}]},
            {"line": 1, "src": [13.0, 15.0], "reveal": "typewriter", "accent": [{"word": "winter", "color": "blue"}]}]
    assert DC.apply_caption_plan(words, plan) == 5
    g = DC.group_words(words, 10.0)
    assert [len(x) for x in g] == [3, 2, 2]                                     # never mixes planned lines
    s0, s1, s2 = (DC.style_group(x, i, i == len(g) - 1) for i, x in enumerate(g))
    assert not s0[0]["drama"] and s0[2]["col"] == DC.GOLD and s0[0]["col"] == DC.WHITE   # Gemini: gold money
    assert s1[0]["drama"] and s1[0]["col"] == DC.BLUE                           # Gemini: typewriter + blue
    assert "planned" not in s2[0] and s2[0]["col"] == DC.RED                    # unplanned -> rules (kill = red)
    assert DC.accent_for("money", 0) == DC.GREEN                                # rules: money -> green


def test_gemini_brief_has_the_ref_breakdown():
    b = sky_ai._arrangement_brief(LINES, 30, "dialogue_drop")
    for k in ("Money Really Talks", "Winter came for House Frey", "punch_line", "typewriter", "green or gold",
              "violence", "hero names", "act2 = 12-30 montage hero shot"):
        assert k in b, k
    assert "song-only edit" in sky_ai._arrangement_brief([], 30, "dialogue_drop")   # no dialogue -> song-only
    assert '"punch_line"' in sky_ai._dialogue_json("dialogue_drop") and sky_ai._dialogue_json("two_act") == ""


# ------------------------------------------------------------------ savage hook
def test_savage_hook_validator():
    assert sky_ai.savage_hook_errors("Money Really Talks", []) == []
    assert sky_ai.savage_hook_errors("Why he cannot stop watching", [])          # soft
    assert sky_ai.savage_hook_errors("He walked in and everyone froze", [])      # 6 words
    assert sky_ai.savage_hook_errors("The aura in this scene is unreal", [])     # banned template still banned
    assert sky_ai.savage_hook_errors("Who runs this city?", [])                  # question
    assert sky_ai.pick_savage_hook(["Watch this moment", "Power Never Asks Twice"], []) == "Power Never Asks Twice"
    assert sky_ai.pick_savage_hook(["Watch this moment"], []) is None            # -> normal hook rules / fallback
    assert sky_ai.savage_wanted([{"act": 1}, {"act": 2}]) and not sky_ai.savage_wanted([{"scene": 1}])
    src = (ROOT / "sky_ai.py").read_text()
    assert "_savage_note(savage)" in src and "hook_alts" in src


# ------------------------------------------------------------------ ONE switch + song-only fallback
def test_one_switch():
    w = edit_mode.dialogue_edit_wanted
    assert w({}, "1") and w({"dialogue_edit": True}, "1") and not w({"dialogue_edit": False}, "1")
    assert not w({"dialogue_edit": True}, "0") and not w({}, "") and not w({}, None) and w({"dialogue_edit": True}, "")
    yml = (ROOT / ".github" / "workflows" / "render.yml").read_text()
    assert "SKY_DIALOGUE_EDIT: ${{ vars.SKY_DIALOGUE_EDIT }}" in yml


def test_fallback_and_targets_wired():
    for s in ("def _dialogue_fallback()", "target = _dialogue_fallback()", "DLG_DEFAULT_TOTAL = 46.0",
              "SONG_TARGET_DUR = max(MIN_UNIQUE_DUR + 0.5, min(_raw_target, 32.0) - END_CARD - 1.5)",
              "_raw_target = DLG_DEFAULT_TOTAL", 'mode=("dialogue_drop" if DIALOGUE_EDIT and EDIT_MODE == "two_act"',
              "dialogue_caps.apply_caption_plan(kept, _plan)", "ref_flash=bool(DIALOGUE_EDIT and is_two_act(edl))",
              "DLG_SHOT_MIN, DLG_SHOT_MAX = 0.28, 0.65", "gem2_rank"):
        assert s in PIPE, s


def test_fallback_resets_globals(monkeypatch):
    pytest.importorskip("cv2")
    pytest.importorskip("scipy")
    import pipeline as P
    monkeypatch.setattr(P, "DIALOGUE_EDIT", True)
    monkeypatch.setattr(P, "EDIT_MODE", "two_act")
    monkeypatch.setattr(P, "SONG_TARGET_DUR", 24.1)
    monkeypatch.setattr(P, "TARGET_DUR", 42.0)
    assert P._dialogue_fallback() == 24.1
    assert P.DIALOGUE_EDIT is False and P.EDIT_MODE == "music_only" and P.TARGET_DUR == 24.1
