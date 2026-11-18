"""QC song-only rule must not apply to love / dialogue_bed edits."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sky_ai


def test_qc_is_song_only_for_every_format():
    # Owner rule: no dialogue anywhere, love included.
    assert sky_ai.qc_audio_mode("music_only", "dialogue_bed", "love") == "music_only"
    assert sky_ai.qc_audio_mode("music_only", "music_only", "love") == "music_only"
    assert sky_ai.qc_audio_mode("", "dialogue_bed", "") == "music_only"
    assert sky_ai.qc_audio_mode("music_only", "music_only", "action") == "music_only"
    assert sky_ai.qc_audio_mode("two_act", "two_act", "action") == "music_only"
