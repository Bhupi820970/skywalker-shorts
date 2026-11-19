import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import caption_patterns as CP  # noqa: E402
import story_captions as sc  # noqa: E402

LAY = {"plate_x": 0, "plate_y": 656, "plate_w": 1080, "plate_h": 608}
ITEM = {"id": "20261121-0900-frieren", "title": "She outlived everyone she loved | Frieren | #edit",
        "description": "She outlived everyone she loved. From official Frieren clips. Edit by Skywalker.\n"
                       "Source: official studio/distributor clip.\n#edit #shorts #anime\n\nMusic:\nSong: X - Y"}


def test_about_one_in_three():
    n = sum(sc.wanted({"id": f"2026112{d}-{h:04d}-x"}) for d in range(10) for h in range(0, 2400, 100))
    assert 0.2 < n / 240 < 0.45


def test_force_and_env():
    assert sc.wanted({"id": "a", "caption_style": "neon"})
    assert not sc.wanted({"id": "a", "caption_style": "none"})
    assert sc.wanted({"id": "a", "caption_style": "none"}, env="1")
    assert not sc.wanted({"id": "a", "caption_style": "neon"}, env="0")


def test_lines_are_our_story_not_lyrics_or_boilerplate():
    lines = sc.story_lines("Why she never cried", ITEM["title"], ITEM["description"])
    blob = " ".join(lines).lower()
    assert lines[0] == "Why she never cried"
    for bad in ("edit by", "source", "#", "song:", "music"):
        assert bad not in blob


def test_ass_look_and_safe_placement(tmp_path):
    beats = [i * 0.5 for i in range(80)]
    out = sc.build(item=ITEM, item_id=ITEM["id"], hook="Why she never cried", beats=beats, dur=28.0, lay=LAY,
                   video_for_faces=None, out_ass=tmp_path / "s.ass")
    assert out and out["pattern"] in CP.BY_ID
    txt = (tmp_path / "s.ass").read_text()
    assert "Anton" in txt and r"\fscx80\fscy80\t(0,90,\fscx100\fscy100)" in txt
    assert "&H00C42EFF&" in txt or out["pattern"] not in ("P01",)  # hot pink present in palettes
    assert sc.qa(out["boxes"], LAY) == []
    for b in out["boxes"]:
        assert b["t0"] >= 1.3 and b["t1"] <= 28.0 - 1.0


def test_never_over_a_face():
    pat = CP.get("P01")
    face = (300, 800, 200, 220)
    pl = sc.place(["NEVER", "CRIED"], pat, LAY, [face], 0)
    assert pl and sc._overlap(pl["box"], face) <= 0.02 * pl["box"][2] * pl["box"][3]
    assert pl["box"][0] > face[0] + face[2] - 5  # beside, on the side away from the face (right)
