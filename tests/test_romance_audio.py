"""Romance edits keep dialogue and duck the instrumental under speech."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import edit_mode
import vocal_mix
import dialogue


def test_love_format_defaults_to_dialogue_bed():
    assert edit_mode.choose_edit_mode("any-id-would-be-music-only", fmt="love") == "dialogue_bed"
    assert edit_mode.choose_edit_mode("any-id-would-be-music-only", fmt="romance") == "dialogue_bed"
    # action titles still use the 3/4 hash split
    assert edit_mode.choose_edit_mode("z" * 8, fmt="action") in ("music_only", "two_act")
    # explicit override still wins
    assert edit_mode.choose_edit_mode("x", override="music_only", fmt="love") == "music_only"


def test_romance_duck_db_is_18_to_22_under():
    os.environ["SKY_EDIT_FORMAT"] = "love"
    os.environ.pop("SKY_LOVE_DUCK_DB", None)
    try:
        db = vocal_mix.current_duck_db(romance=True)
        assert -22 <= db <= -18
        os.environ["SKY_LOVE_DUCK_DB"] = "-30"
        assert vocal_mix.current_duck_db(romance=True) == -22.0
        os.environ["SKY_LOVE_DUCK_DB"] = "-10"
        assert vocal_mix.current_duck_db(romance=True) == -18.0
        # relative to the default bed, speech-window music is about -20 dB
        os.environ["SKY_LOVE_DUCK_DB"] = "-20"
        vol = dialogue.music_speech_vol(dialogue.MUSIC_BED_VOL)
        ratio = vol / dialogue.MUSIC_BED_VOL
        import math
        assert abs(20 * math.log10(ratio) - -20) < 0.2
    finally:
        os.environ.pop("SKY_EDIT_FORMAT", None)
        os.environ.pop("SKY_LOVE_DUCK_DB", None)


def test_silencedetect_finds_speech_and_gaps(tmp_path):
    """ffmpeg silencedetect (via dialogue.vad_windows) marks speech vs quiet."""
    wav = tmp_path / "src.wav"
    # 0.0-0.6s silence, 0.6-1.4s tone (speech stand-in), 1.4-2.0s silence
    import subprocess
    subprocess.check_call([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "aevalsrc=0:s=44100",
        "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=44100",
        "-filter_complex",
        "[0]atrim=0:0.6,asetpts=PTS-STARTPTS[a];"
        "[1]atrim=0:0.8,asetpts=PTS-STARTPTS[b];"
        "[0]atrim=0:0.6,asetpts=PTS-STARTPTS[c];"
        "[a][b][c]concat=n=3:v=0:a=1[out]",
        "-map", "[out]", str(wav),
    ])
    wins = dialogue.vad_windows(wav)
    assert wins, "expected at least one speech window"
    # The tone around 0.6-1.4s should be covered
    covered = any(a <= 0.9 <= b or (a < 1.2 and b > 0.8) for a, b in wins)
    assert covered, wins
