"""Owner rule: no character voice/dialogue in any format; only our song plays."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import edit_mode
import sky_ai


def _func_src(path: Path, name: str) -> str:
    src = path.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} not found in {path.name}")


def test_every_format_and_override_is_music_only():
    for fmt in ("action", "love", "romance", "gta6", "", None):
        for ov in (None, "dialogue_bed", "two_act", "music_only"):
            assert edit_mode.choose_edit_mode("id", ov, fmt=fmt, iconic_hook=True) == "music_only"
        assert edit_mode.default_edit_mode("id", fmt) == "music_only"


def test_mux_final_never_mixes_source_audio():
    body = _func_src(ROOT / "pipeline.py", "mux_final")
    assert "_mix_source_and_music(" not in body
    assert "_mix_two_act(" not in body
    assert "_mix_music_only(" in body


def test_love_dialogue_switch_is_off():
    body = _func_src(ROOT / "pipeline.py", "is_love_dialogue")
    assert "return False" in body


def test_render_item_forces_music_only():
    src = (ROOT / "scripts" / "render_item.py").read_text()
    assert 'mode = "dialogue_bed" if fmt == "love"' not in src
    assert 'mode = "music_only"' in src


def test_qc_never_expects_dialogue():
    assert sky_ai.qc_audio_mode("dialogue_bed", "dialogue_bed", "love") == "music_only"
