"""No scene clip goes into two Shorts: atomic clip ledger (clip_reserve.py), no network.

A render reserves each clip the moment it picks it; parallel renders (matrix max-parallel 4) can
never get the same clip. Rendered -> used for good; failed -> released; stale reservations expire
after 8 h; an unreachable ledger means the clip is skipped (fail closed)."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import clip_reserve as cr  # noqa: E402
import posted_history as ph  # noqa: E402

REL = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/"


@pytest.fixture
def be(tmp_path, monkeypatch):
    p = tmp_path / "clip_reservations.json"
    monkeypatch.setenv("SKY_RESERVE_FILE", str(p))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return cr.LocalBackend(p)


def _r(owner, be, video=""):
    return cr.Reservations(owner, backend=be, log=lambda *a: None, tries=2, video=video)


def test_keys_never_collapse_different_videos():
    assert cr.base_id(REL + "JJK_scene2_h264.mp4") == "jjk_scene2_h264.mp4"
    assert cr.base_id("gh:" + REL + "jjk_scene2_h264.mp4") == "jjk_scene2_h264.mp4"
    a = cr.base_id("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    b = cr.base_id("https://youtu.be/bbbbbbbbbbb?t=4")
    assert a == "yt:aaaaaaaaaaa" and b == "yt:bbbbbbbbbbb"


def test_two_parallel_renders_cannot_take_the_same_clip(be):
    r1, r2 = _r("run-1-a", be), _r("run-1-b", be)
    assert r1.reserve(REL + "x_scene1_h264.mp4")
    assert not r2.reserve("gh:" + REL + "X_scene1_h264.mp4")
    assert "x_scene1_h264.mp4" in r2.taken_ids()
    assert r1.reserve(REL + "x_scene1_h264.mp4")          # idempotent for the owner


def test_rendered_clip_is_used_for_good(be):
    r1 = _r("run-1-a", be, video="a")
    r1.reserve(REL + "x_scene1_h264.mp4")
    r1.reserve(REL + "x_scene2_h264.mp4")
    r1.confirm([REL + "x_scene1_h264.mp4"], video="a")
    r1.release()
    later = _r("run-9-z", be, video="z")
    assert not later.reserve(REL + "x_scene1_h264.mp4")   # used, forever
    assert later.reserve(REL + "x_scene2_h264.mp4")       # unused clip was given back
    doc = json.loads(Path(be.path).read_text())
    assert doc["clips"]["x_scene1_h264.mp4"]["state"] == "used"


def test_re_render_of_the_same_video_may_reuse_its_own_clips(be):
    _r("run-1-a", be, video="a").confirm(["x_scene1_h264.mp4"], video="a")
    assert _r("run-2-a", be, video="a").reserve("x_scene1_h264.mp4")
    assert not _r("run-2-b", be, video="b").reserve("x_scene1_h264.mp4")


def test_failed_render_releases_its_clips(be):
    r1 = _r("run-1-a", be)
    r1.reserve("x_scene1_h264.mp4")
    r1.release()
    assert _r("run-2-b", be).reserve("x_scene1_h264.mp4")


def test_stale_reservation_expires_after_8h(be):
    r1 = _r("run-1-a", be)
    r1.reserve("x_scene1_h264.mp4")
    doc, sha = be.read()
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cr.RESERVE_TTL_H + 0.5)
    doc["clips"]["x_scene1_h264.mp4"]["at"] = old.isoformat(timespec="seconds")
    be.write(doc, sha, "age")
    assert _r("run-2-b", be).reserve("x_scene1_h264.mp4")


def test_unreachable_ledger_skips_the_clip(be):
    class Down:
        def locked(self):
            raise OSError("down")

        def read(self):
            raise OSError("down")
    r = _r("run-1-a", Down())
    assert not r.reserve("x_scene1_h264.mp4")
    assert r.ok is False


def test_conflict_is_retried_compare_and_swap(be):
    class Racy(cr.LocalBackend):
        n = 0

        def write(self, doc, sha, msg):
            if self.n == 0:
                self.n += 1
                raise cr.Conflict()
            return super().write(doc, sha, msg)
    r = cr.Reservations("run-1-a", backend=Racy(be.path), log=lambda *a: None, tries=3)
    assert r.reserve("x_scene1_h264.mp4")


def test_blocked_clips_and_refill_count_treat_ledger_clips_as_gone(be, tmp_path):
    _r("run-1-a", be, video="q1").confirm([REL + "x_scene1_h264.mp4"], video="q1")
    _r("run-2-b", be).reserve(REL + "x_scene2_h264.mp4")
    root = tmp_path / "repo"
    (root / "results").mkdir(parents=True)
    gone = ph.blocked_clips(root, rows=[], reserved=True)
    assert {"x_scene1_h264.mp4", "x_scene2_h264.mp4"} <= gone
    assert "x_scene1_h264.mp4" not in ph.blocked_clips(root, rows=[], exclude_ids={"q1"}, reserved=True)
    import title_swap
    fresh = title_swap.unposted_release_assets(
        {"x_scene1_h264.mp4", "x_scene2_h264.mp4", "x_scene3_h264.mp4"}, root, reserved=True)
    assert fresh == {"x_scene3_h264.mp4"}


def test_rendered_unposted_queue_short_holds_its_clips():
    rows = [{"id": "2026-11-27-a", "status": "done", "source_bundle": ["y_scene1_h264.mp4"]},
            {"id": "2026-11-27-b", "status": "failed", "source_bundle": ["y_scene2_h264.mp4"]}]
    held = ph.reserved_clips(rows)
    assert "y_scene1_h264.mp4" in held and "y_scene2_h264.mp4" not in held


def test_render_item_wires_the_ledger():
    src = (ROOT / "scripts" / "render_item.py").read_text()
    assert "RESERVE.reserve(u)" in src and "_settle_reservations(r)" in src
    assert "blocked_clips(REPO, exclude_ids={iid}, reserved=True)" in src
