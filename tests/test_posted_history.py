"""No video, and no clip from the same source scene, is ever posted twice (no network).

25 Nov: Evil Eye (dandadanevil_scene1) had already gone out on 17 and 24 Nov; backup stock
kept cutting the same scene (stock-dandadanevil-04..11) and the 72h source guard let the
Nov 24 repost through."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import backup_stock  # noqa: E402
import buffer_post  # noqa: E402
import posted_history as ph  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
REL = "gh:https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/"
STOCK = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/backup-stock/"


def _w(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def _root(tmp_path, monkeypatch=None):
    for d in ("config", "results", "queue"):
        (tmp_path / d).mkdir(exist_ok=True)
    _w(tmp_path / "config" / "backup_pool.json", {"items": []})
    _w(tmp_path / "config" / "backup_stock.json", {"target": 30, "items": []})
    if monkeypatch is not None:
        monkeypatch.setenv("SKY_ROOT", str(tmp_path))
        monkeypatch.setattr(backup_stock, "REPO", tmp_path)
    return tmp_path


def _posted(iid, clip, title, **kw):
    r = {"id": iid, "status": "done", "slot": "2026-11-17T09:00:00+05:30", "title": title,
         "source": REL + clip, "asset_url": f"https://x/render-{iid}/{iid}.mp4",
         "youtube_url": f"https://youtube.com/shorts/{iid}", "buffer_status": "sent"}
    r.update(kw)
    return r


def _stock(iid, clip, **kw):
    r = {"id": iid, "status": "done", "title": f"Hook {iid}", "hook": "Hook", "source": REL + clip,
         "asset_url": STOCK + f"{iid}.mp4", "stock": True, "no_publish": False,
         "stock_topic": "backup-" + backup_stock.parse_stock_id(iid)[0], "qc_status": "pass"}
    r.update(kw)
    return r


def test_same_source_scene_blocked_with_a_different_title(tmp_path):
    root = _root(tmp_path)
    _w(root / "results" / "a.json", _posted("a", "dandadanevil_scene1_h264.mp4",
                                            "Okarun finally loses control of his power"))
    idx = ph.index(root)
    again = {"id": "b", "source": REL + "dandadanevil_scene1_h264.mp4",
             "title": "Evil Eye backstory in Dan Da Dan hits different", "asset_url": STOCK + "stock-x-04.mp4"}
    assert "same source scene" in ph.conflict(again, idx)
    other = dict(again, source=REL + "suzume_scene1_h264.mp4")
    assert ph.conflict(other, idx) is None
    # its own record never blocks itself
    assert ph.conflict(json.loads((root / "results" / "a.json").read_text()), idx) is None


def test_any_clip_of_the_merged_bundle_counts(tmp_path):
    root = _root(tmp_path)
    _w(root / "results" / "a.json", _posted("a", "evil_scene1_h264.mp4", "t1",
                                            source_bundle=["evil_scene1_h264.mp4", "evil_scene3_h264.mp4"]))
    idx = ph.index(root)
    assert ph.conflict({"id": "b", "source": REL + "evil_scene3_h264.mp4"}, idx)
    assert ph.conflict({"id": "c", "source": REL + "evil_scene2_h264.mp4"}, idx) is None


def test_same_file_or_bytes_blocked(tmp_path):
    root = _root(tmp_path)
    _w(root / "results" / "s.json", _posted("s", "x_scene1_h264.mp4", "t", asset_url=STOCK + "stock-x-01.mp4",
                                            asset_sha256="ABC"))
    idx = ph.index(root)
    assert "same video file" in ph.conflict({"id": "z", "asset_url": STOCK + "stock-x-01.mp4"}, idx)
    assert "same video bytes" in ph.conflict({"id": "z"}, idx, sha="abc")


def test_blocked_unposted_results_do_not_count(tmp_path):
    root = _root(tmp_path)
    r = _posted("a", "evil_scene1_h264.mp4", "t", publish_blocked="repeat: x")
    r.pop("youtube_url"); r.pop("buffer_status")
    _w(root / "results" / "a.json", r)
    _w(root / "results" / "demo-v1.json", _posted("demo-v1", "evil_scene1_h264.mp4", "t", no_publish=True))
    assert ph.index(root) == {}


def test_seed_expands_legacy_posts_to_whole_topic_and_is_durable(tmp_path):
    root = _root(tmp_path)
    _w(root / "results" / "a.json", _posted("a", "dandadanevil_scene1_h264.mp4", "t"))
    ph.seed(root, {"dandadanevil_scene1_h264.mp4", "dandadanevil_scene3_h264.mp4", "dandadan_scene1_h264.mp4"})
    (root / "results" / "a.json").unlink()  # history file alone still blocks
    idx = ph.index(root)
    assert idx["clip:dandadanevil_scene3_h264.mp4"] == "a"
    assert "clip:dandadan_scene1_h264.mp4" not in idx


def test_record_appends_history(tmp_path):
    root = _root(tmp_path)
    ph.record({"id": "p1", "source": REL + "k_scene1_h264.mp4", "asset_url": "https://x/p1.mp4",
               "youtube_url": "https://youtube.com/shorts/p1"}, root, sha="ff")
    h = ph.load_history(root)
    assert h["items"][0]["id"] == "p1"
    assert {"clip:k_scene1_h264.mp4", "asset:p1.mp4", "sha:ff"} <= set(h["items"][0]["keys"])


def test_repeat_of_has_no_72h_window():
    now = datetime(2026, 11, 24, 9, 0, tzinfo=IST)
    old = _posted("20261117-0900-dandadanevil", "dandadanevil_scene1_h264.mp4", "Okarun finally loses control")
    old["buffer_posted_at"] = "2026-11-17T03:30:00.000Z"
    idx = buffer_post.posted_index([old], [])
    r = {"id": "20261124-0900-dandadanevil", "slot": now.isoformat(), "title": "Evil Eye backstory hits different",
         "source": REL + "dandadanevil_scene1_h264.mp4"}
    assert "same source scene" in buffer_post.repeat_of(r, idx)
    assert "same source scene" in buffer_post.repeat_of(r, [], hist=ph.index(rows=[old], root=Path("/nonexistent")))


def test_ready_stock_is_one_per_scene_and_never_posted(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    now = datetime(2026, 11, 25, 15, 0, tzinfo=IST)
    _w(root / "results" / "p.json", _posted("20261124-0900-evil", "evil_scene1_h264.mp4", "posted"))
    items = []
    for iid, clip in (("stock-evil-10", "evil_scene1_h264.mp4"), ("stock-evil-11", "evil_scene1_h264.mp4"),
                      ("stock-suzume-01", "suzume_scene1_h264.mp4"), ("stock-suzume-02", "suzume_scene1_h264.mp4"),
                      ("stock-kgf-01", "kgf_scene2_h264.mp4")):
        _w(root / "results" / f"{iid}.json", _stock(iid, clip))
        slug = backup_stock.parse_stock_id(iid)[0]
        items.append({"id": iid, "slug": slug, "topic_id": f"backup-{slug}", "status": "ready",
                      "asset_url": STOCK + f"{iid}.mp4"})
    _w(root / "config" / "backup_stock.json", {"target": 30, "items": items})
    man = backup_stock.sync(root, now)
    st = {e["id"]: e["status"] for e in man["items"]}
    assert st["stock-evil-10"] == st["stock-evil-11"] == "dup_scene"   # scene already on the channel
    assert st["stock-suzume-01"] == "ready" and st["stock-suzume-02"] == "dup_scene"  # one per scene
    assert st["stock-kgf-01"] == "ready"
    # videos are kept (marked, never deleted)
    assert (root / "results" / "stock-evil-10.json").exists()
    ent = backup_stock.pick_stock(man, root, "2026-11-25", now=now, buffer_posts=[])
    assert ent["id"] in ("stock-suzume-01", "stock-kgf-01")


def test_assigned_slot_blocks_same_scene_for_the_next_slot(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    now = datetime(2026, 11, 25, 15, 0, tzinfo=IST)
    # 16:00 already holds stock-evil-10 (assigned, not yet posted)
    _w(root / "results" / "20261125-1600-evil.json",
       dict(_stock("stock-evil-10", "evil_scene1_h264.mp4"), id="20261125-1600-evil",
            slot="2026-11-25T16:00:00+05:30", assigned_from_stock=True, stock_id="stock-evil-10"))
    _w(root / "results" / "stock-evil-10.json", _stock("stock-evil-10", "evil_scene1_h264.mp4",
                                                       stock_used=True, assigned_slot="x"))
    _w(root / "results" / "stock-evil-11.json", _stock("stock-evil-11", "evil_scene1_h264.mp4"))
    _w(root / "config" / "backup_stock.json", {"target": 30, "items": [
        {"id": "stock-evil-11", "slug": "evil", "topic_id": "backup-evil", "status": "ready",
         "asset_url": STOCK + "stock-evil-11.mp4"}]})
    man = backup_stock.sync(root, now)
    assert man["items"][-1]["status"] == "dup_scene"
    assert backup_stock.assign_to_slot(root, "2026-11-25T18:30:00+05:30", now) is None


def test_refill_one_copy_per_topic(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    _w(root / "config" / "backup_pool.json", {"items": [
        {"id": "backup-evil", "fallback_asset": "evil_scene1_h264.mp4"},
        {"id": "backup-kgf", "fallback_asset": "kgf_scene1_h264.mp4"}]})
    _w(root / "results" / "stock-evil-01.json", _stock("stock-evil-01", "evil_scene1_h264.mp4"))
    _w(root / "config" / "backup_stock.json", {"target": 30, "max_refill_per_run": 2, "items": [
        {"id": "stock-evil-01", "slug": "evil", "topic_id": "backup-evil", "status": "ready",
         "asset_url": STOCK + "stock-evil-01.mp4"}]})
    assert backup_stock.next_refill_ids(root, limit=2) == ["stock-kgf-01"]


def test_release_pool_drops_topics_whose_clips_are_all_posted(tmp_path, monkeypatch):
    import sourcing
    import title_swap
    root = _root(tmp_path, monkeypatch)
    monkeypatch.setattr(backup_stock, "_repo", lambda: root)
    monkeypatch.setattr(sourcing, "release_only", lambda: True)
    monkeypatch.setattr(sourcing, "release_source_assets", lambda *a, **k: {
        "evil_scene1_h264.mp4", "evil_scene3_h264.mp4", "kgf_scene1_h264.mp4", "kgf_scene2_h264.mp4"})
    monkeypatch.setattr(title_swap, "release_clip_health", lambda root=None, **k: {})
    monkeypatch.setattr(title_swap, "release_title_blocklist", lambda root=None: {})
    _w(root / "results" / "p.json", _posted("p", "evil_scene1_h264.mp4", "t",
                                            source_bundle=["evil_scene1_h264.mp4", "evil_scene3_h264.mp4"]))
    pool = [{"id": "backup-evil", "fallback_asset": "evil_scene1_h264.mp4"},
            {"id": "backup-kgf", "fallback_asset": "kgf_scene1_h264.mp4"}]
    assert [b["id"] for b in backup_stock.release_backed_pool(pool)] == ["backup-kgf"]


def test_plan_skips_replaced_ids_from_a_stale_dispatch(tmp_path):
    root = _root(tmp_path)
    _w(root / "queue" / "2026-11-25.json", [
        {"id": "20261125-2030-dandadanevil", "slot": "2026-11-25T20:30:00+05:30", "status": "replaced"},
        {"id": "20261125-1830-witchhatatelier", "slot": "2026-11-25T18:30:00+05:30", "status": "queued"}])
    env = dict(os.environ, IDS="20261125-1830-witchhatatelier,20261125-2030-dandadanevil",
               PLAN_NOW="2026-11-25T16:30:00+05:30", GITHUB_OUTPUT=str(root / "out.txt"))
    env.pop("GH_TOKEN", None)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "plan.py")], cwd=root, env=env, check=True,
                   capture_output=True, text=True, timeout=60)
    out = (root / "out.txt").read_text()
    assert '"20261125-1830-witchhatatelier"' in out and "dandadanevil" not in out
