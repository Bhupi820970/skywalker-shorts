#!/usr/bin/env python3
"""Pre-rendered backup stock: refill, assign, and same-slot posting guards."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup_stock
import plan

IST = timezone(timedelta(hours=5, minutes=30))


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def _ready_result(iid: str, **extra):
    r = {
        "id": iid,
        "status": "done",
        "asset_url": f"https://github.com/Bhupi820970/skywalker-shorts/releases/download/backup-stock/{iid}.mp4",
        "title": f"Hook for {iid}",
        "hook": "A cold open that slaps",
        "description": "From the official trailer. Edit by Skywalker.",
        "no_publish": False,
    }
    r.update(extra)
    return r


def _root(tmp_path: Path, monkeypatch, pool=None, manifest=None):
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    monkeypatch.setattr(backup_stock, "REPO", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "queue").mkdir()
    _write(tmp_path / "config" / "backup_pool.json", {
        "items": pool or [
            {"id": "backup-zootopia2", "fallback_asset": "zootopia2_scene1_h264.mp4"},
            {"id": "backup-kalki2898ad", "fallback_asset": "kalki2898ad_scene1_h264.mp4"},
            {"id": "backup-superman", "fallback_asset": "superman_scene1_h264.mp4"},
        ]
    })
    _write(tmp_path / "config" / "backup_stock.json", manifest or {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "release": "backup-stock", "items": []
    })
    return tmp_path


def test_refill_when_empty_picks_two_diverse_ids(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    ids = backup_stock.next_refill_ids(root, limit=2)
    assert len(ids) == 2
    assert all(backup_stock.parse_stock_id(i) for i in ids)
    slugs = {backup_stock.parse_stock_id(i)[0] for i in ids}
    assert len(slugs) == 2  # different topics in one batch


def test_no_refill_when_at_target(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    items = []
    for i in range(30):
        slug = "zootopia2" if i < 15 else "superman"
        n = (i % 15) + 1
        iid = f"stock-{slug}-{n:02d}"
        _write(root / "results" / f"{iid}.json", _ready_result(iid, stock_topic=f"backup-{slug}"))
        items.append({"id": iid, "slug": slug, "topic_id": f"backup-{slug}",
                      "status": "ready", "asset_url": _ready_result(iid)["asset_url"]})
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": items
    })
    assert backup_stock.next_refill_ids(root, limit=2) == []
    assert backup_stock.status_summary(root)["need_refill"] is False


def test_refill_while_below_target(tmp_path, monkeypatch):
    # 6 ready used to be above the old floor of 5; refill must still run toward 30.
    root = _root(tmp_path, monkeypatch)
    items = []
    for i in range(5):
        iid = f"stock-zootopia2-{i+1:02d}"
        _write(root / "results" / f"{iid}.json", _ready_result(iid, stock_topic="backup-zootopia2"))
        items.append({"id": iid, "slug": "zootopia2", "topic_id": "backup-zootopia2",
                      "status": "ready", "asset_url": _ready_result(iid)["asset_url"]})
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": items
    })
    assert backup_stock.status_summary(root)["need_refill"] is True
    ids = backup_stock.next_refill_ids(root, limit=2)
    assert len(ids) == 2
    # already has stock-zootopia2-01..05, next zootopia2 would be 06; other topics start at 01
    assert "stock-zootopia2-01" not in ids


def test_assign_marks_used_and_never_reuses(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    _write(root / "results" / "stock-kalki2898ad-01.json",
           _ready_result("stock-kalki2898ad-01", stock_topic="backup-kalki2898ad"))
    slot = "2026-11-14T16:00:00+05:30"
    now = datetime(2026, 11, 14, 15, 10, tzinfo=IST)
    got = backup_stock.assign_to_slot(root, slot, now)
    assert got is not None
    assert got["id"] == "20261114-1600-kalki2898ad"
    assert got["slot"] == slot
    assert got["assigned_from_stock"] is True
    assert got["stock_id"] == "stock-kalki2898ad-01"
    assert got.get("buffer_post_id") is None
    src = json.loads((root / "results" / "stock-kalki2898ad-01.json").read_text())
    assert src["stock_used"] is True
    assert src["assigned_slot"] == slot
    # second assign must not reuse it
    got2 = backup_stock.assign_to_slot(root, "2026-11-14T18:30:00+05:30", now)
    assert got2 is None


def test_assign_avoids_same_topic_same_day(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    _write(root / "results" / "stock-zootopia2-01.json",
           _ready_result("stock-zootopia2-01", stock_topic="backup-zootopia2"))
    _write(root / "results" / "stock-superman-01.json",
           _ready_result("stock-superman-01", stock_topic="backup-superman"))
    # a live zootopia2 already occupies today's queue
    _write(root / "queue" / "2026-11-14.json", [
        {"id": "20261114-1230-zootopia2", "slot": "2026-11-14T12:30:00+05:30", "status": "queued"}
    ])
    now = datetime(2026, 11, 14, 15, 10, tzinfo=IST)
    got = backup_stock.assign_to_slot(root, "2026-11-14T16:00:00+05:30", now)
    assert got["stock_id"] == "stock-superman-01"
    assert "zootopia2" not in got["id"]


def test_slot_needs_stock_on_failed_or_imminent(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    slot = "2026-11-14T16:00:00+05:30"
    _write(root / "queue" / "2026-11-14.json", [
        {"id": "20261114-1600-fantasticfour", "slot": slot, "status": "queued"}
    ])
    now = datetime(2026, 11, 14, 10, 0, tzinfo=IST)  # 6h before, not imminent
    assert backup_stock.slot_needs_stock(root, slot, now) is False
    _write(root / "results" / "20261114-1600-fantasticfour.json",
           {"id": "20261114-1600-fantasticfour", "status": "failed", "slot": slot})
    assert backup_stock.slot_needs_stock(root, slot, now) is True
    # a ready primary covers it
    _write(root / "results" / "20261114-1600-fantasticfour.json",
           _ready_result("20261114-1600-fantasticfour", slot=slot))
    assert backup_stock.slot_needs_stock(root, slot, now) is False
    # T-60 with nothing ready
    _write(root / "results" / "20261114-1600-fantasticfour.json",
           {"id": "20261114-1600-fantasticfour", "status": "retry_queued", "slot": slot})
    late = datetime(2026, 11, 14, 15, 10, tzinfo=IST)
    assert backup_stock.slot_needs_stock(root, slot, late) is True
    # no_publish must not count as coverage
    _write(root / "results" / "20261114-1600-fantasticfour.json",
           _ready_result("20261114-1600-fantasticfour", slot=slot, no_publish=True,
                         asset_url="https://github.com/x/releases/download/test-renders/x.mp4"))
    assert backup_stock.slot_needs_stock(root, slot, late) is True


def test_no_publish_does_not_cover_slot(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    slot = "2026-11-14T16:00:00+05:30"
    _write(root / "results" / "20261114-1600-fantasticfour.json",
           _ready_result("20261114-1600-fantasticfour", slot=slot, no_publish=True,
                         asset_url="https://github.com/x/releases/download/test-renders/x.mp4"))
    assert backup_stock.slot_is_covered(root, slot) is False


def test_slot_post_conflicts_prefers_assigned_stock():
    stock = {"id": "20261114-1600-superman", "slot": "s", "assigned_from_stock": True}
    primary = {"id": "20261114-1600-fantasticfour", "slot": "s"}
    posted = {"id": "other", "slot": "s", "buffer_post_id": "bp1"}
    assert backup_stock.slot_post_conflicts(primary, [stock, primary]) is True
    assert backup_stock.slot_post_conflicts(stock, [stock, primary]) is False
    assert backup_stock.slot_post_conflicts(primary, [posted, primary]) is True


def test_plan_skips_backup_render_when_stock_covers(tmp_path, monkeypatch):
    now = datetime(2026, 11, 14, 15, 10, tzinfo=IST)
    slot = "2026-11-14T16:00:00+05:30"
    items = {"ff": {"id": "ff", "slot": slot, "status": "queued"}}
    results = {"ff": {"status": "failed"}}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    monkeypatch.setattr(backup_stock, "REPO", tmp_path)
    (tmp_path / "results").mkdir()
    (tmp_path / "config").mkdir()
    _write(tmp_path / "results" / "20261114-1600-superman.json",
           _ready_result("20261114-1600-superman", slot=slot, assigned_from_stock=True,
                         stock_id="stock-superman-01"))
    # Other official times are already queued so this test only checks the
    # failed 16:00 slot, which stock already covers.
    for hhmm, iid in (("11:30", "a"), ("13:30", "b"), ("18:30", "c"), ("20:30", "d")):
        iso = f"2026-11-14T{hhmm}:00+05:30"
        items[iid] = {"id": iid, "slot": iso, "status": "queued"}
        results[iid] = {"status": "retry_queued"}
    pool = [{"id": "backup-zootopia2"}]
    picks, _ = plan.select_slot_backups(
        items, lambda i: results.get(i), pool, set(), now, ahead=6, busy=set()
    )
    assert picks == []


def test_plan_accepts_forced_stock_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "queue").mkdir()
    (tmp_path / "queue" / "day.json").write_text("[]\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "config" / "backup_pool.json").write_text('{"items": []}\n')
    monkeypatch.setenv("IDS", "stock-alienromulus-01,stock-avatarfireandash-01")
    monkeypatch.setenv("MAX_PER_RUN", "2")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("BACKUPS_ONLY", raising=False)
    monkeypatch.delenv("RETRY_FAILED", raising=False)
    plan.main()
    out = capsys.readouterr()
    text = out.out + out.err
    assert "stock-alienromulus-01" in text
    assert "stock-avatarfireandash-01" in text


def test_missing_official_slot_gets_stock_before_lead(tmp_path, monkeypatch):
    """Official 18:30 with nothing in the queue must be assigned immediately,
    not only inside the 60-minute window (that narrow window is how 14 Nov missed)."""
    root = _root(tmp_path, monkeypatch)
    _write(root / "results" / "stock-superman-01.json",
           _ready_result("stock-superman-01", stock_topic="backup-superman"))
    _write(root / "results" / "stock-zootopia2-01.json",
           _ready_result("stock-zootopia2-01", stock_topic="backup-zootopia2"))
    # A third topic not already in today's queue, so 18:30 is not left empty
    # when same-day slugs are skipped.
    _write(root / "results" / "stock-twisters-01.json",
           _ready_result("stock-twisters-01", stock_topic="backup-twisters"))
    # Real 14 Nov shape: leftover 12:30/17:30, official 13:30 and 18:30 missing.
    q = [
        ("06:30", "howtorobabank"), ("09:00", "runningman"), ("11:30", "mandalorian"),
        ("12:30", "zootopia2"), ("16:00", "fantasticfour"), ("17:30", "kalki2898ad"),
        ("20:30", "mortalkombat2"), ("22:30", "rajinikanth"),
    ]
    rows = []
    for hhmm, slug in q:
        iid = f"20261114-{hhmm.replace(':','')}-{slug}"
        iso = f"2026-11-14T{hhmm}:00+05:30"
        rows.append({"id": iid, "slot": iso, "status": "queued"})
        # Earlier/queued slots already have a publishable video; only the
        # official holes should consume stock.
        _write(root / "results" / f"{iid}.json", _ready_result(iid, slot=iso))
    _write(root / "queue" / "2026-11-14.json", rows)
    now = datetime(2026, 11, 14, 15, 10, tzinfo=IST)  # ~3h before 18:30
    slot_1830 = "2026-11-14T18:30:00+05:30"
    slot_1330 = "2026-11-14T13:30:00+05:30"
    assert backup_stock.slot_needs_stock(root, slot_1830, now) is True
    assert slot_1830 in backup_stock.upcoming_slots(root, now)
    got = backup_stock.assign_needed(root, now)
    slots = {g["slot"] for g in got}
    assert slot_1830 in slots
    assert slot_1330 in slots
    assert backup_stock.slot_is_covered(root, slot_1830) is True
    assert backup_stock.slot_is_covered(root, slot_1330) is True


def test_queued_unready_waits_until_lead(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    slot = "2026-11-15T06:30:00+05:30"
    _write(root / "queue" / "2026-11-15.json", [
        {"id": "20261115-0630-werwulf", "slot": slot, "status": "queued",
         "fallback_asset": "werwulf_trailer_h264.mp4"}
    ])
    now = datetime(2026, 11, 15, 0, 15, tzinfo=IST)  # more than 60 min before
    assert backup_stock.slot_needs_stock(root, slot, now) is False
    late = datetime(2026, 11, 15, 5, 40, tzinfo=IST)
    assert backup_stock.slot_needs_stock(root, slot, late) is True


def test_plan_backup_for_missing_official_only_when_stock_empty(tmp_path, monkeypatch):
    now = datetime(2026, 11, 15, 0, 15, tzinfo=IST)
    slot = "2026-11-15T13:30:00+05:30"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    monkeypatch.setattr(backup_stock, "REPO", tmp_path)
    (tmp_path / "results").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "queue").mkdir(exist_ok=True)
    # Official 13:30 (and 18:30) missing; every other official time is queued.
    official_queued = ["06:30", "09:00", "11:30", "16:00", "20:30", "22:30"]
    extras = ["12:30", "17:30"]
    items = {}
    qrows = []
    for hhmm in official_queued + extras:
        iso = f"2026-11-15T{hhmm}:00+05:30"
        iid = f"20261115-{hhmm.replace(':','')}-x"
        items[iid] = {"id": iid, "slot": iso, "status": "queued"}
        qrows.append(items[iid])
    _write(tmp_path / "queue" / "2026-11-15.json", qrows)
    pool = [{"id": "backup-twisters", "fallback_asset": "twisters_scene1_h264.mp4"}]
    _write(tmp_path / "config" / "backup_pool.json", {"items": pool})
    _write(tmp_path / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": []
    })
    # Tight window so only 13:30 (not 18:30) is due.
    now = datetime(2026, 11, 15, 12, 45, tzinfo=IST)
    picks, _ = plan.select_slot_backups(
        items, lambda i: None, pool, set(), now, ahead=2, busy=set()
    )
    assert picks == ["backup-twisters@20261115-1330"]

    # Once a ready stock video exists, plan must not start an on-demand render.
    _write(tmp_path / "results" / "stock-superman-01.json",
           _ready_result("stock-superman-01", stock_topic="backup-superman"))
    _write(tmp_path / "config" / "backup_stock.json", {
        "target": 30, "items": [{
            "id": "stock-superman-01", "slug": "superman",
            "topic_id": "backup-superman", "status": "ready",
            "asset_url": _ready_result("stock-superman-01")["asset_url"],
        }]
    })
    picks2, _ = plan.select_slot_backups(
        items, lambda i: None, pool, set(), now, ahead=2, busy=set()
    )
    assert picks2 == []


def test_assign_skips_title_used_in_last_7_days(tmp_path, monkeypatch):
    """A stock video whose title/topic already went out in the last 7 days must
    not be assigned; Buffer would swallow the new slot by title match."""
    root = _root(tmp_path, monkeypatch)
    _write(root / "results" / "stock-zootopia2-01.json",
           _ready_result("stock-zootopia2-01", stock_topic="backup-zootopia2",
                         title="Varang's aura in this scene is terrifying #avatar #varang #scifi"))
    _write(root / "results" / "stock-superman-01.json",
           _ready_result("stock-superman-01", stock_topic="backup-superman",
                         title="Superman takes flight in this scene #superman #dc #action"))
    # Yesterday's posted Avatar (same title as zootopia2 stock, different day)
    _write(root / "results" / "20261114-1230-avatarfireandash.json", {
        "id": "20261114-1230-avatarfireandash",
        "slot": "2026-11-14T12:30:00+05:30",
        "status": "done",
        "title": "Varang's aura in this scene is terrifying #avatar #varang #scifi",
        "asset_url": "https://example.com/a.mp4",
        "assigned_from_stock": True,
        "stock_id": "stock-avatarfireandash-01",
        "buffer_post_id": "bp-old",
        "buffer_status": "sent",
        "buffer_due_at": "2026-11-14T07:12:45.371Z",
    })
    now = datetime(2026, 11, 15, 0, 30, tzinfo=IST)
    got = backup_stock.assign_to_slot(root, "2026-11-15T13:30:00+05:30", now)
    assert got is not None
    assert got["stock_id"] == "stock-superman-01"
    assert "zootopia2" not in got["id"]
    assert got["title"] != "Varang's aura in this scene is terrifying #avatar #varang #scifi"


def test_title_match_miss_does_not_cover_slot(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    slot = "2026-11-15T13:30:00+05:30"
    _write(root / "results" / "20261115-1330-avatarfireandash.json",
           _ready_result("20261115-1330-avatarfireandash", slot=slot,
                         title="Varang's aura in this scene is terrifying #avatar #varang #scifi",
                         assigned_from_stock=True, stock_id="stock-avatarfireandash-02",
                         buffer_post_id="bp-old", buffer_status="sent",
                         buffer_due_at="2026-11-14T07:12:45.371Z",
                         buffer_note="matched existing post by title"))
    assert backup_stock.is_title_match_miss(
        json.loads((root / "results" / "20261115-1330-avatarfireandash.json").read_text()), slot
    ) is True
    assert backup_stock.slot_is_covered(root, slot) is False
    _write(root / "results" / "stock-superman-01.json",
           _ready_result("stock-superman-01", stock_topic="backup-superman",
                         title="Superman takes flight in this scene #superman #dc #action"))
    now = datetime(2026, 11, 15, 0, 30, tzinfo=IST)
    got = backup_stock.assign_to_slot(root, slot, now)
    assert got is not None
    assert got["id"] == "20261115-1330-superman"
    assert got["stock_id"] == "stock-superman-01"
    assert backup_stock.slot_is_covered(root, slot) is True
    # the swallowed Avatar result must not block Buffer from posting the new one
    assert backup_stock.slot_post_conflicts(
        got, [
            json.loads((root / "results" / "20261115-1330-avatarfireandash.json").read_text()),
            got,
        ]
    ) is False


def test_same_slot_title_match_is_real_coverage():
    r = {"buffer_note": "matched existing post by title",
         "slot": "2026-11-15T13:30:00+05:30",
         "buffer_due_at": "2026-11-15T08:00:00.000Z"}
    assert backup_stock.is_title_match_miss(r) is False
    r2 = {"buffer_note": "matched existing post by title",
          "slot": "2026-11-15T13:30:00+05:30",
          "buffer_due_at": "2026-11-14T07:12:45.371Z"}
    assert backup_stock.is_title_match_miss(r2) is True

def test_trailer_stock_is_not_ready_and_not_assigned(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    _write(root / "results" / "stock-wickedforgood-01.json", _ready_result(
        "stock-wickedforgood-01",
        stock_topic="backup-wickedforgood",
        source="gh:https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/wickedforgood_trailer_h264.mp4",
        title="The moment Elphaba chose her own path",
    ))
    r = json.loads((root / "results" / "stock-wickedforgood-01.json").read_text())
    assert backup_stock.is_ready_result(r) is False
    slot = "2026-11-17T13:30:00+05:30"
    now = datetime(2026, 11, 17, 12, 0, tzinfo=IST)
    got = backup_stock.assign_to_slot(root, slot, now)
    assert got is None
    # already-used/posted results are not touched
    posted = _ready_result(
        "20261117-1330-wickedforgood",
        slot=slot, status="done",
        source="gh:https://github.com/x/sources/wickedforgood_trailer_h264.mp4",
        assigned_from_stock=True,
    )
    _write(root / "results" / "20261117-1330-wickedforgood.json", posted)
    again = json.loads((root / "results" / "20261117-1330-wickedforgood.json").read_text())
    assert again["status"] == "done"
    assert again.get("assigned_from_stock") is True


def test_pick_stock_skips_song_used_in_last_3_days(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    now = datetime(2026, 11, 17, 16, 0, tzinfo=IST)
    _write(root / "results" / "20261117-1130-cyberpunkedgerunners.json", {
        "id": "20261117-1130-cyberpunkedgerunners",
        "status": "done",
        "slot": "2026-11-17T11:30:00+05:30",
        "music": "sw_metamorphosis.mp3",
        "title": "David did not expect this from Lucy",
    })
    _write(root / "results" / "stock-gladiator2-01.json", _ready_result(
        "stock-gladiator2-01", stock_topic="backup-gladiator2",
        music="sw_metamorphosis.mp3",
        title="A unique gladiator hook",
        source="gh:https://github.com/x/sources/gladiator2_scene1_h264.mp4",
    ))
    _write(root / "results" / "stock-superman-01.json", _ready_result(
        "stock-superman-01", stock_topic="backup-superman",
        music="sw_otherphonk.mp3",
        title="A unique superman hook",
        source="gh:https://github.com/x/sources/superman_scene1_h264.mp4",
    ))
    man = backup_stock.sync(root, now)
    # force both ready
    for e in man["items"]:
        if e["id"].startswith("stock-"):
            e["status"] = "ready"
            e["asset_url"] = _ready_result(e["id"])["asset_url"]
    backup_stock.save_manifest(man, root)
    picked = backup_stock.pick_stock(man, root, "2026-11-17", now=now)
    assert picked is not None
    assert picked["id"] == "stock-superman-01"


def test_trailer_only_pool_item_is_not_preuploaded():
    assert backup_stock.has_preuploaded({
        "id": "backup-wickedforgood",
        "fallback_asset": "wickedforgood_trailer_h264.mp4",
        "fallback_mp4": [
            "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/wickedforgood_trailer_h264.mp4"
        ],
    }) is False
    assert backup_stock.has_preuploaded({
        "id": "backup-dandadan",
        "fallback_asset": "dandadan_scene1_h264.mp4",
    }) is True



def test_source_quality_reason_below_1080():
    import backup_stock as b
    assert b.source_quality_reason({"source_w": 1280, "source_h": 720}) == "1280x720 below 1080p"
    assert b.source_quality_reason({"width": 1920, "height": 1080}) is None
    assert b.source_quality_reason({}) is None  # unknown size is not a silent reject


def test_must_not_publish_blocks_demo_and_test_renders():
    assert backup_stock.must_not_publish({"id": "demo-v5-romance", "status": "done",
        "asset_url": "https://example/x.mp4", "no_publish": True})
    assert backup_stock.must_not_publish({"id": "demo-v5-romance", "demo": True,
        "asset_url": "https://example/x.mp4", "status": "done"})
    assert backup_stock.must_not_publish({"id": "stock-x-01", "status": "done",
        "asset_url": "https://github.com/o/r/releases/download/test-renders/test-demo.mp4"})
    assert backup_stock.must_not_publish({"id": "test-foo", "status": "done",
        "asset_url": "https://example/x.mp4"})
    assert not backup_stock.must_not_publish(_ready_result("stock-zootopia2-01"))


def test_retire_and_skip_broken_topics_after_repeated_logo_or_404(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch, pool=[
        {"id": "backup-bleachtybw", "fallback_asset": "bleachtybw_scene1_h264.mp4"},
        {"id": "backup-zootopia2", "fallback_asset": "zootopia2_scene1_h264.mp4"},
    ])
    for n in (1, 2):
        iid = f"stock-bleachtybw-{n:02d}"
        _write(root / "results" / f"{iid}.json", {
            "id": iid, "status": "failed", "source_unrecoverable": True,
            "last_error": "reject logo/watermark: gh:sources/bleachtybw_scene1_h264.mp4",
        })
    # tooling-only fail must not retire a different topic
    _write(root / "results" / "stock-zootopia2-01.json", {
        "id": "stock-zootopia2-01", "status": "failed",
        "last_error": "yt-dlp:ERROR: Fixed output name but more than one file to download",
    })
    newly = backup_stock.retire_broken_topics(root)
    slugs = {r["slug"] for r in newly}
    assert "bleachtybw" in slugs
    assert "zootopia2" not in slugs
    assert "bleachtybw" in backup_stock.retired_slugs(root)
    ids = backup_stock.next_refill_ids(root, limit=2)
    assert ids
    assert all(not i.startswith("stock-bleachtybw-") for i in ids)
    assert any(i.startswith("stock-zootopia2-") for i in ids)


def test_fixed_output_name_alone_is_not_retire_worthy():
    assert not backup_stock.is_retire_worthy_error(
        "yt-dlp:ERROR: Fixed output name but more than one file to download: out.mp4")
    assert backup_stock.is_retire_worthy_error(
        "Fixed output name but more than one file | reject logo/watermark: gh:x.mp4")
    assert backup_stock.is_retire_worthy_error("HTTP Error 404: Not Found")
    # GitHub bot-check is a download problem (the box fetches), never a reason to retire the topic.
    assert not backup_stock.is_retire_worthy_error("Sign in to confirm you're not a bot")
    assert not backup_stock.is_retire_worthy_error("no result JSON; job cancelled or timed out")


def test_plan_prioritizes_upcoming_over_stale_past(tmp_path, monkeypatch, capsys):
    """A cap of 4 must go to today's official slots, not 14h-late yesterday items.

    This is how the 07:25 IST cron on 20 Nov spent itself on 19 Nov 17:30-22:30
    and never started today's 06:30/09:00/11:30/13:30 renders.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    monkeypatch.setattr(backup_stock, "REPO", tmp_path)
    (tmp_path / "queue").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "config").mkdir()
    _write(tmp_path / "config" / "backup_pool.json", {"items": []})
    scene = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources/{}_scene1_h264.mp4"
    rows = []
    for day, times in (
        ("2026-11-19", ["17:30", "18:30", "20:30", "22:30"]),
        ("2026-11-20", ["06:30", "09:00", "11:30", "13:30"]),
    ):
        for hhmm in times:
            slug = f"t{day[-2:]}{hhmm.replace(':', '')}"
            iid = f"{day.replace('-', '')}-{hhmm.replace(':', '')}-{slug}"
            rows.append({
                "id": iid,
                "slot": f"{day}T{hhmm}:00+05:30",
                "status": "queued",
                "fallback_asset": f"{slug}_scene1_h264.mp4",
                "fallback_mp4": [scene.format(slug)],
                "sources": [scene.format(slug)],
            })
    _write(tmp_path / "queue" / "2026-11-20.json", rows)
    monkeypatch.setenv("PLAN_NOW", "2026-11-20T07:25:00+05:30")
    monkeypatch.setenv("MAX_PER_RUN", "4")
    monkeypatch.setenv("RENDER_AHEAD_HOURS", "8")
    monkeypatch.delenv("IDS", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("BACKUPS_ONLY", raising=False)
    monkeypatch.delenv("RETRY_FAILED", raising=False)
    monkeypatch.delenv("PLAN_PAST_HOURS", raising=False)
    plan.main()
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "20261120-0630-" in text
    assert "20261120-0900-" in text
    assert "20261120-1130-" in text
    assert "20261120-1330-" in text
    assert "20261119-1730-" not in text
    assert "20261119-2230-" not in text


def test_stale_pending_does_not_block_refill(tmp_path, monkeypatch):
    """Pending older than PENDING_HOURS must not count toward the target."""
    root = _root(tmp_path, monkeypatch)
    now = datetime(2026, 11, 25, 11, 0, tzinfo=IST)
    items = []
    for i in range(5):
        iid = f"stock-zootopia2-{i+1:02d}"
        _write(root / "results" / f"{iid}.json", _ready_result(iid, stock_topic="backup-zootopia2"))
        items.append({"id": iid, "slug": "zootopia2", "topic_id": "backup-zootopia2",
                      "status": "ready", "asset_url": _ready_result(iid)["asset_url"]})
    # 25 stale pending used to make ready+pending look like the target of 30.
    for i in range(25):
        items.append({
            "id": f"stock-superman-{i+1:02d}",
            "slug": "superman",
            "topic_id": "backup-superman",
            "status": "pending",
            "queued_at": "2026-11-16 02:24 IST",
        })
    # One still in-flight pending should keep counting.
    items.append({
        "id": "stock-kalki2898ad-01",
        "slug": "kalki2898ad",
        "topic_id": "backup-kalki2898ad",
        "status": "pending",
        "queued_at": "2026-11-25 10:11 IST",
    })
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": items
    })
    summary = backup_stock.status_summary(root, now)
    assert summary["ready"] == 5
    assert summary["pending"] == 1
    assert summary["need_refill"] is True
    ids = backup_stock.next_refill_ids(root, limit=2, now=now)
    # Stale superman pending no longer counts, so superman refills. Zootopia (5 ready)
    # and kalki (1 in flight) already hold a copy of their scenes: one per topic.
    assert ids == ["stock-superman-26"]
    assert "stock-superman-01" not in ids
    assert "stock-kalki2898ad-01" not in ids


def test_fresh_pending_still_counts_toward_target(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    now = datetime(2026, 11, 25, 11, 0, tzinfo=IST)
    items = []
    for i in range(5):
        iid = f"stock-zootopia2-{i+1:02d}"
        _write(root / "results" / f"{iid}.json", _ready_result(iid, stock_topic="backup-zootopia2"))
        items.append({"id": iid, "slug": "zootopia2", "topic_id": "backup-zootopia2",
                      "status": "ready", "asset_url": _ready_result(iid)["asset_url"]})
    for i in range(25):
        items.append({
            "id": f"stock-superman-{i+1:02d}",
            "slug": "superman",
            "topic_id": "backup-superman",
            "status": "pending",
            "queued_at": "2026-11-25 10:00 IST",
        })
    _write(root / "config" / "backup_stock.json", {
        "target": 30, "refill_at": 5, "max_refill_per_run": 2, "items": items
    })
    summary = backup_stock.status_summary(root, now)
    assert summary["pending"] == 25
    assert summary["need_refill"] is False
    assert backup_stock.next_refill_ids(root, limit=2, now=now) == []


def test_unassigned_stock_song_does_not_block_itself(tmp_path, monkeypatch):
    """An unassigned stock render must not count as a recently used song (27 Nov 2026: every ready stock
    video blocked itself, so no stock was ever given to a missed slot)."""
    _root(tmp_path, monkeypatch)
    now = datetime(2026, 11, 27, 11, 0, tzinfo=IST)
    at = "2026-11-27 08:00 IST"
    _write(tmp_path / "results" / "stock-a-01.json",
           _ready_result("stock-a-01", music="song_a.mp3", at=at))
    _write(tmp_path / "results" / "20261127-0900-b.json",
           _ready_result("20261127-0900-b", music="song_b.mp3", at=at, slot="2026-11-27T09:00:00+05:30"))
    used = backup_stock._recent_music_files(tmp_path, now)
    assert "song_b.mp3" in used
    assert "song_a.mp3" not in used
