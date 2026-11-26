import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import box_service as bs  # noqa: E402
import music_lib  # noqa: E402


def test_resolve_url_scene_only():
    item = {"source_url": "https://www.youtube.com/watch?v=abcdefghijk"}
    assert bs.resolve_url({"asset": "x_scene1_h264.mp4", "item": item}, {}) == item["source_url"]
    assert bs.resolve_url({"asset": "x_scene2_h264.mp4", "item": item}, {}) == ""
    tr = {"x_scene1_h264.mp4": "https://www.youtube.com/watch?v=trailerxxxx&t=trailer"}
    assert bs.resolve_url({"asset": "x_scene1_h264.mp4", "item": {}}, tr) == ""
    assert not bs.ASSET_RX.match("x_trailer_h264.mp4")


def test_pre_check_rejects_trailers_logo_channels_and_low_res():
    ok = {"title": "Gon vs Pitou", "channel": "VIZ Media", "duration": 120,
          "formats": [{"vcodec": "avc1", "height": 1080}]}
    assert bs.pre_check("https://www.youtube.com/watch?v=abcdefghijk", ok) is None
    assert "trailer" in bs.pre_check("u", dict(ok, title="Official Trailer"))
    assert "channel" in bs.pre_check("u", dict(ok, channel="Crunchyroll"))
    assert "1080" in bs.pre_check("u", dict(ok, formats=[{"vcodec": "avc1", "height": 720}]))
    assert bs.pre_check("u", None)


def test_browser_auth_is_opt_in(monkeypatch):
    monkeypatch.delenv("SKY_YTDLP_BROWSER", raising=False)
    assert bs.browser_args() == []
    monkeypatch.setenv("SKY_YTDLP_BROWSER", "chrome:/x;rm -rf /")
    assert bs.browser_args() == []
    monkeypatch.setenv("SKY_YTDLP_BROWSER", "chrome:/home/box/chrome-profile/Default")
    assert bs.browser_args() == ["--cookies-from-browser", "chrome:/home/box/chrome-profile/Default"]


def test_js_runtime_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("SKY_YTDLP_JS_RUNTIME", raising=False)
    monkeypatch.setattr(bs.os, "access", lambda *a, **k: False)
    assert bs.js_runtime_args() == []
    monkeypatch.setenv("SKY_YTDLP_JS_RUNTIME", "deno:/x;rm -rf /")
    assert bs.js_runtime_args() == []
    monkeypatch.setenv("SKY_YTDLP_JS_RUNTIME", "deno:/home/box/.deno/bin/deno")
    assert bs.js_runtime_args() == ["--js-runtimes", "deno:/home/box/.deno/bin/deno"]


def test_dispatch_off_by_default(monkeypatch):
    monkeypatch.delenv("SKY_BOX_DISPATCH", raising=False)
    called = []
    monkeypatch.setattr(bs, "gh", lambda *a, **k: called.append(a))
    bs.step_dispatch({})
    assert called == []


def test_content_id_blocklist_and_credit():
    assert music_lib.is_denied_track({"title": "Give It Up", "artist": "KC & The Sunshine Band"})
    assert music_lib.is_denied_track({"title": "Give It Up", "artist": "KC and the Sunshine Band", "priority": "high"})
    big = {"title": "Some Song", "artist": "X", "credit": "Provided to YouTube by Sony Music Entertainment"}
    assert music_lib.is_denied_track(big)
    assert not music_lib.is_denied_track(dict(big, priority="high"))
    assert music_lib.is_denied_track({"title": "x", "artist": "y", "channel": "T-Series"})
    c = music_lib.credit_block({"artist": "NSASS", "title": "Run", "source": "archive.org", "source_id": "abc",
                                "license": "https://creativecommons.org/licenses/by/4.0/"})
    assert "Song: NSASS - Run" in c and "https://archive.org/details/abc" in c and "License:" in c


def test_library_has_no_banned_or_blocked_songs_in_picker():
    lib = json.loads((Path(__file__).resolve().parents[1] / "music" / "library.json").read_text())
    for t in lib["tracks"]:
        blob = (t.get("title", "") + " " + t.get("artist", "")).lower()
        if "goofy ahh" in blob or "godless" in blob or "187_notor1ous" in blob:
            assert music_lib.is_denied_track(t)


def test_download_error_is_short_and_pathless():
    import box_service as b
    why = b.download_error("[youtube] x: Downloading\nERROR: [youtube] abcdefghijk: Video unavailable. This video is private")
    assert why == "Video unavailable. This video is private"
    assert "<path>" in b.download_error("ERROR: unable to open /home/someone/x/y.mp4")


def test_permanent_reasons_mark_asset_dead_for_two_weeks(monkeypatch):
    import box_service as b
    st = {}
    monkeypatch.setattr(b, "today", lambda: "2026-11-26")
    assert b.PERMANENT.search("logo/subtitle-burn channel: Crunchyroll")
    assert not b.PERMANENT.search("HTTP Error 429: Too Many Requests")
    b.mark_dead(st, "x_scene1_h264.mp4", "logo/subtitle-burn channel: Crunchyroll")
    assert b.is_dead(st, "x_scene1_h264.mp4")
    monkeypatch.setattr(b, "today", lambda: "2026-12-11")
    assert not b.is_dead(st, "x_scene1_h264.mp4")


def test_quality_gate_is_codec_aware():
    import clip_fetch as c
    vp9 = {"w": 1920, "h": 1080, "codec": "vp9", "kbps": 968, "dur": 60}
    h264_low = {"w": 1920, "h": 1080, "codec": "h264", "kbps": 968, "dur": 60}
    assert c.quality_ok(vp9) is None
    assert "low bitrate" in c.quality_ok(h264_low)
    assert "vcodec:h264" in c.SORT and "tbr" in c.SORT
