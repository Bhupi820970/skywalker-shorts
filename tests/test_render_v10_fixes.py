"""v10 regressions: our caption bar flagged as a logo, endless fall-through, crash with no result."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gate
import logo_guard as lg


@pytest.fixture
def vertical(tmp_path):
    """270x480 vertical frame: white caption bar (rows 120-155) right above a test-pattern plate."""
    p = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=270x150:rate=10",
                    "-t", "3", "-vf", "pad=270:480:0:156:black,drawbox=x=0:y=120:w=270:h=36:color=white:t=fill",
                    "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def test_final_scan_masks_our_caption_bar(vertical, tmp_path):
    # layout.json coords are in the 1080x1920 master: bar rows 480-624 == 120-156 at 480 high.
    layout = {"bar_y": 480, "bar_h": 144}
    seen = {}

    def ask(prompt, imgs):
        from PIL import Image
        im = Image.open(sorted((tmp_path / "logo_final").glob("f*.jpg"))[0]).convert("RGB")
        w, h = im.size
        seen["bar"] = im.getpixel((w // 2, int(h * 138 / 480)))
        return {"flagged": []}

    r = lg.scan_video(vertical, tmp_path, ask=ask, layout=layout)
    assert r["ok"] is True and r["masked_rows"]
    assert all(100 < c < 160 for c in seen["bar"]), seen  # grey, not white: Gemini never sees our text


def test_final_scan_autodetects_bar_without_layout(vertical, tmp_path):
    r = lg.scan_video(vertical, tmp_path, ask=lambda p, i: {"flagged": []})
    assert r["ok"] is True and r.get("masked_rows"), r


def test_in_scene_text_is_not_a_logo_but_brands_are():
    assert not lg.real_hit("STAFF text on shirt")
    assert not lg.real_hit("grey blank band at top")
    assert lg.real_hit("Netflix logo")
    assert lg.real_hit("copyright text and TOHO animation watermark")
    assert lg.real_hit("Crunchyroll logo and show title text")
    assert lg.real_hit("burned-in subtitle")
    assert lg.real_hit("Netflix logo on a shirt")


def test_unmappable_tile_index_still_blocks(vertical, tmp_path):
    r = lg.scan_video(vertical, tmp_path, ask=lambda p, i: {"flagged": [{"i": 999, "what": "Netflix N"}]},
                      layout={"bar_y": 480, "bar_h": 144})
    assert r["ok"] is False


def test_transient_miss_is_retried_once(vertical, tmp_path):
    calls = []

    def ask(p, i):
        calls.append(1)
        return None if len(calls) == 1 else {"flagged": []}

    r = lg.scan_video(vertical, tmp_path, ask=ask, layout={"bar_y": 480, "bar_h": 144})
    assert r["ok"] is True and len(calls) == 2


def test_gate_marks_missing_logo_verdict_as_pending_only(vertical):
    item = {"title": "Why she never looked back", "description": "d", "hook": "She never looked back"}
    g = gate.run_gate(vertical, qa_obj={}, checks={}, item=item, work=None, gemini_qc=None, out_res=1080,
                      logo_scan=lambda f: {"ok": None, "error": "no gemini verdict"})
    assert not g["ok"]
    assert any("no verdict" in f for f in g["failures"])
    hit = gate.run_gate(vertical, qa_obj={}, checks={}, item=item, work=None, gemini_qc=None, out_res=1080,
                        logo_scan=lambda f: {"ok": False, "hits": {"final": ["1.0s Netflix N"]}})
    assert hit["logo_pending_only"] is False


def test_render_item_has_hard_caps_below_step_timeout():
    src = (ROOT / "scripts" / "render_item.py").read_text()
    wf = (ROOT / ".github" / "workflows" / "render.yml").read_text()
    import re
    step = max(int(x) for x in re.findall(r"timeout-minutes:\s*(\d+)", wf) if int(x) < 180)
    soft = float(re.search(r'SKY_JOB_BUDGET_MIN", "(\d+)"', src).group(1))
    hard = float(re.search(r'SKY_HARD_CAP_MIN", "(\d+)"', src).group(1))
    assert soft < hard <= step - 8
    assert "signal.alarm" in src and "_crash_result" in src and "MAX_SOURCES_PER_JOB" in src


def test_retired_stock_topic_writes_terminal_result(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    shutil.copy(ROOT / "scripts" / "render_item.py", repo / "scripts" / "render_item.py")
    (repo / "config" / "backup_pool.json").write_text(json.dumps({"items": []}))
    r = subprocess.run([sys.executable, str(repo / "scripts" / "render_item.py"), "stock-gonetopic-03"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    res = json.loads((repo / "results" / "stock-gonetopic-03.json").read_text())
    assert res["status"] == "failed" and res["retired"] is True
