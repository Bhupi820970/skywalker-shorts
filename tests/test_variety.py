"""Variety rule (BiBi 25 Nov): rotate categories, never the same franchise twice in a day, ~3 days apart."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import backup_stock  # noqa: E402
import rebalance_queue as rq  # noqa: E402
import variety as v  # noqa: E402

IST = v.IST


def _w(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1) + "\n")


def test_franchise_groups_seasons_and_spinoffs():
    assert {v.franchise(s) for s in ("dandadan", "dandadan2", "dandadanevil", "dandadanlove")} == {"dandadan"}
    assert {v.franchise(s) for s in ("jjk0", "jujutsukaisencullinggame", "jjkgojovstoji")} == {"jjk"}
    assert v.franchise("thedarkknight") == v.franchise("thebatman") == "batman"
    assert v.franchise("cyberpunk2077") == v.franchise("cyberpunkedgerunners")
    assert v.franchise({"id": "20261126-0630-kaiju8s2"}) == "kaiju8"
    assert v.franchise("donkey") != v.franchise("don2")


def test_category_inference():
    assert v.category({"slug": "cyberpunk2077", "pillar": "anime"}) == "game"
    assert v.category({"slug": "arcane2", "pillar": "anime"}) == "cartoon"
    assert v.category({"slug": "spiderverse"}) == "cartoon"
    assert v.category({"slug": "strangerthings", "pillar": "hollywood"}) == "tv"
    assert v.category({"slug": "dandadan", "pillar": "anime"}) == "anime"
    assert v.category({"slug": "pathaan", "pillar": "bollywood"}) == "movie"
    assert v.category({"slug": "x", "category": "game", "pillar": "anime"}) == "game"


def _ctx(*rows):
    return [{"id": f"r{i}", "dt": dt, "franchise": fr, "category": cat} for i, (dt, fr, cat) in enumerate(rows)]


def test_penalty_rules():
    d = datetime(2026, 11, 26, 13, 30, tzinfo=IST)
    ctx = _ctx((d.replace(hour=6, minute=30), "dandadan", "anime"),
               (d.replace(hour=11, minute=30), "batman", "movie"),
               (d - timedelta(days=2), "arcane", "cartoon"))
    assert v.penalty("dandadan", "anime", d, ctx) == v.FORBIDDEN       # same day: never
    assert v.penalty("arcane", "cartoon", d, ctx) == v.P_FRANCHISE_NEAR  # 2 days: soft
    assert v.penalty("rrr", "movie", d, ctx) == v.P_SAME_CATEGORY        # neighbour is a movie
    assert v.penalty("frieren", "anime", d, ctx) == 0


def _queue_root(tmp_path: Path) -> Path:
    days = {
        "2026-11-26": [("0630", "dandadan2"), ("0900", "dandadanevil"), ("1130", "jjk0"), ("1330", "arcane2")],
        "2026-11-27": [("0630", "dandadan"), ("0900", "thebatman"), ("1130", "jujutsukaisencullinggame"),
                       ("1330", "rrr")],
    }
    for d, rows in days.items():
        _w(tmp_path / "queue" / f"{d}.json", [
            {"id": f"{d.replace('-', '')}-{t}-{s}", "slot": f"{d}T{t[:2]}:{t[2:]}:00+05:30", "status": "queued",
             "hook": f"hook {s}", "title": f"title {s}", "pillar": "anime"} for t, s in rows])
    _w(tmp_path / "config" / "topic_catalog.json", {"items": [
        {"slug": "kgf", "pillar": "south", "hook": "h", "title": "t"},
        {"slug": "eldenring", "category": "game", "hook": "h", "title": "t"},
        {"slug": "spiderverse", "hook": "h", "title": "t"},
        {"slug": "interstellar", "pillar": "hollywood", "hook": "h", "title": "t"},
        {"slug": "godofwar", "category": "game", "hook": "h", "title": "t"},
        {"slug": "strangerthings", "hook": "h", "title": "t"},
        {"slug": "kungfupanda", "hook": "h", "title": "t"},
        {"slug": "frieren", "pillar": "anime"},
        {"slug": "dandadan2", "pillar": "anime"}, {"slug": "arcane2", "pillar": "anime"}]})
    return tmp_path


def _mk(day, hhmm, t, assets):
    return {"id": f"{day.replace('-', '')}-{hhmm.replace(':', '')}-{t['slug']}",
            "slot": f"{day}T{hhmm}:00+05:30", "status": "queued", "category": t.get("category")}


def test_rebalance_obeys_rules_and_keeps_in_flight_items(tmp_path):
    root = _queue_root(tmp_path)
    # 26 Nov 06:30 is already rendering -> must not move
    _w(root / "results" / "20261126-0630-dandadan2.json", {"id": "20261126-0630-dandadan2", "status": "rendering"})
    assets = {f"{s}_scene{i}_h264.mp4" for s in ("kgf", "eldenring", "spiderverse", "interstellar", "godofwar", "strangerthings", "kungfupanda")
              for i in (1, 2)}
    now = datetime(2026, 11, 25, 17, 0, tzinfo=IST)
    res = rq.rebalance(root, ["2026-11-26", "2026-11-27"], now, assets, make_item=_mk)
    d26 = res["days"]["2026-11-26"]
    assert d26[0]["id"] == "20261126-0630-dandadan2"  # in flight: untouched
    cat = v.load_catalog(root)
    for d, items in res["days"].items():
        frs = [v.franchise(it) for it in items]
        assert len(frs) == len(set(frs)), (d, frs)  # never the same franchise twice a day
        cats = [v.category(it, cat) for it in items]
        assert all(a != b for a, b in zip(cats, cats[1:])), (d, cats)  # neighbours differ here
    assert len(res["days"]["2026-11-26"]) == 4 and len(res["days"]["2026-11-27"]) == 4


def test_backup_stock_skips_same_franchise_and_prefers_other_category(tmp_path, monkeypatch):
    root = _queue_root(tmp_path)
    man = {"items": [
        {"id": "stock-dandadanlove-01", "slug": "dandadanlove", "status": "ready"},
        {"id": "stock-frieren-01", "slug": "frieren", "status": "ready"},
        {"id": "stock-kgf-01", "slug": "kgf", "status": "ready"},
    ]}
    monkeypatch.setattr(backup_stock, "ready_entries", lambda m: m["items"])
    monkeypatch.setattr(backup_stock, "recent_titles_and_topics", lambda *a, **k: (set(), set()))
    monkeypatch.setattr(backup_stock, "posted_hook_keys", lambda *a, **k: set())
    now = datetime(2026, 11, 26, 9, 0, tzinfo=IST)
    # 11:30 slot (jjk0 failed): neighbours are dandadanevil (anime, 09:00) and arcane2 (13:30)
    ent = backup_stock.pick_stock(man, root, "2026-11-26", now=now, slot_s="2026-11-26T11:30:00+05:30")
    assert ent["slug"] == "kgf"  # dandadan* blocked for the day; anime next to anime avoided


def test_fill_queue_avoids_same_day_franchise(tmp_path, monkeypatch):
    import fill_queue
    root = tmp_path
    _w(root / "queue" / "2026-11-30.json", [
        {"id": "20261130-0630-dandadan2", "slot": "2026-11-30T06:30:00+05:30", "status": "queued", "pillar": "anime"}])
    cat = [{"slug": s, "pillar": p, "hook": f"Why {s} hits different", "title": f"Why {s} hits different | x | #edit",
            "description": "d", "tags": [s], "scene_assets": [f"{s}_scene1_h264.mp4"], "category": c}
           for s, p, c in (("dandadanevil", "anime", "anime"), ("dandadanlove", "anime", "anime"),
                           ("kgf", "south", "movie"), ("frieren", "anime", "anime"),
                           ("eldenring", "game", "game"), ("spiderverse", "movie", "cartoon"),
                           ("strangerthings", "tv", "tv"), ("rrr", "south", "movie"), ("bluelock", "anime", "anime"))]
    _w(root / "config" / "topic_catalog.json", {"items": cat})
    monkeypatch.setattr(fill_queue, "REPO", root)
    monkeypatch.setattr(fill_queue, "QUEUE", root / "queue")
    monkeypatch.setattr(fill_queue.backup_stock, "blocked_slugs", lambda *a, **k: set())
    monkeypatch.setattr(fill_queue.hook_text, "check_item_hook_fields", lambda it: None)
    monkeypatch.setattr(fill_queue.perf, "load_perf", lambda *a, **k: {})
    have = {t["scene_assets"][0] for t in cat}
    fill_queue.fill_day("2026-11-30", cat, set(), have, set())
    items = json.loads((root / "queue" / "2026-11-30.json").read_text())
    frs = [v.franchise(it) for it in items]
    assert len(items) > 1 and len(frs) == len(set(frs)), frs
