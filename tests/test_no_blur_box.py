"""v2 style rule: never a blur rectangle over burned-in subs; captions stay on a clean picture."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_blur_rectangle_filters_in_render_code():
    for name in ("pipeline.py", "dialogue_caps.py", "story_captions.py", "scripts/render_item.py"):
        src = (ROOT / name).read_text()
        assert not re.search(r"delogo|boxblur|smartblur|inpaint", src), name


def test_end_card_is_off_and_tail_freeze_qc_stays():
    import pipeline
    import auto_qc
    assert pipeline.END_CARD == 0.0
    assert callable(auto_qc.check_tail_freeze)
