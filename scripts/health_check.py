#!/usr/bin/env python3
"""Verify the next 24h of IST slots have a ready video (or a renderable backup).

Every official IST slot is checked, plus any extra slot already in the queue
(legacy 12:30/17:30 included). A slot is healthy when:
  - a results/<id>.json for that slot is publishable (done + asset_url), OR
  - health just assigned a ready backup-stock video to it, OR
  - a queued item for that slot still has a sources-release fallback (can render).

An official slot missing from the queue is not treated as covered just because
the on-demand backup pool is non-empty; those pool renders are only planned
when stock is empty. If short, write config/health_status.json so the workflow
can dispatch an on-demand backup-pool render. Never deletes videos.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
import backup_stock  # noqa: E402
import slot_times  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
GH = "https://github.com/Bhupi820970/skywalker-shorts/releases/download/sources"


def load_pool() -> list[dict]:
    try:
        return json.loads((REPO / "config" / "backup_pool.json").read_text()).get("items") or []
    except Exception:
        return []


def result_for(iid: str) -> dict | None:
    p = REPO / "results" / f"{iid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def queue_items() -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(REPO / "queue" / "2026-*.json"))):
        try:
            out.extend(json.load(open(f)))
        except Exception:
            pass
    return out


def has_preuploaded(it: dict) -> bool:
    if it.get("fallback_asset") or it.get("fallback_mp4"):
        return True
    return False


def main() -> int:
    now = datetime.now(IST)
    # Auto-retire (never delete) topics that keep failing on logo/404/bot-check.
    backup_stock.retire_broken_topics(REPO, now)
    # Stock whose mp4 is gone from the release is never assigned (an empty release just means "no stock").
    try:
        backup_stock.audit_lost_assets(REPO, now=now)
    except Exception as e:
        print("stock audit skipped:", type(e).__name__)
    horizon = now + timedelta(hours=24)
    items = queue_items()
    pool = load_pool()
    used_backups = set()
    for p in (REPO / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("backup_item"):
            used_backups.add(r["backup_item"])

    # Group by official slot minute in the next 24h
    slots: dict[str, list[dict]] = {}
    for it in items:
        if it.get("status", "queued") not in ("queued", "done"):
            continue
        try:
            slot = datetime.fromisoformat(it["slot"])
        except Exception:
            continue
        if not (now - timedelta(hours=2) <= slot <= horizon):
            continue
        slots.setdefault(it["slot"], []).append(it)

    # Also ensure every official time in the window appears
    day0 = now.date()
    for d in range(0, 2):
        day = (day0 + timedelta(days=d)).isoformat()
        for hhmm in slot_times.official_times():
            iso = slot_times.slot_iso(day, hhmm)
            try:
                dt = datetime.fromisoformat(iso)
            except Exception:
                continue
            if now - timedelta(hours=2) <= dt <= horizon:
                slots.setdefault(iso, [])

    # Assign ready stock first so missing official slots (and failed/QC-rejected
    # items) are covered before we classify short vs ready.
    assigned = backup_stock.assign_needed(REPO, now)
    assigned_by_slot = {g["slot"]: g for g in assigned}
    stock = backup_stock.status_summary(REPO, now)

    short = []
    ready = []
    for slot_s, group in sorted(slots.items()):
        if backup_stock.slot_is_covered(REPO, slot_s):
            how, iid = "done", None
            for it in group:
                r = result_for(it["id"])
                if backup_stock.is_ready_result(r):
                    iid = it["id"]
                    break
            if slot_s in assigned_by_slot:
                iid = assigned_by_slot[slot_s]["id"]
                how = "backup_stock"
            elif iid is None:
                how, iid = "backup_stock", "stock"
            ready.append({"slot": slot_s, "id": iid, "how": how})
            continue
        live = [it for it in group if it.get("status", "queued") == "queued"]

        def _st(it):
            r = result_for(it["id"]) or {}
            if r.get("qc_rejected"):
                return "failed"
            return r.get("status")

        can_render = any(
            has_preuploaded(it) and _st(it) not in ("failed", "done") for it in live
        )
        if can_render:
            ready.append({"slot": slot_s, "id": live[0]["id"], "how": "queued+fallback"})
            continue
        # Failed or missing official slot: the on-demand backup pool can still
        # cover a *failed* queued item (plan.py will render it if stock is empty).
        # A slot with no queue item is short so health dispatches that render;
        # merely having a free pool topic is not coverage (plan never picked it
        # for the missed 18:30 on 14 Nov).
        all_failed = bool(live) and all(_st(it) == "failed" for it in live)
        cand = next((b for b in pool if b["id"] not in used_backups and has_preuploaded(b)), None)
        if cand and all_failed:
            ready.append({"slot": slot_s, "id": cand["id"], "how": "backup_pool"})
            used_backups.add(cand["id"])
            continue
        short.append(slot_s)

    # Extra ready target: at least 2 spare done renders beyond the next 24h live slots
    extra_needed = int(os.environ.get("EXTRA_READY_PER_DAY", "2"))
    spare_done = []
    for p in (REPO / "results").glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("status") == "done" and r.get("asset_url") and r.get("spare"):
            spare_done.append(r["id"])

    preuploaded_pool = [b for b in pool if has_preuploaded(b)]
    status = {
        "at": now.strftime("%Y-%m-%d %H:%M IST"),
        "slots_checked": len(slots),
        "ready": ready,
        "short": short,
        "backup_pool_size": len(pool),
        "backup_pool_preuploaded": len(preuploaded_pool),
        "spare_done": spare_done,
        "extra_ready_target": extra_needed,
        "stock_ready": stock["ready"],
        "stock_pending": stock["pending"],
        "stock_used": stock["used"],
        "stock_target": stock["target"],
        "stock_need_refill": stock["need_refill"],
        "stock_assigned": [g["id"] for g in assigned],
        "ok": not short and len(preuploaded_pool) >= 20,
    }
    out = REPO / "config" / "health_status.json"
    out.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: status[k] for k in ("slots_checked", "short", "backup_pool_preuploaded",
                                             "stock_ready", "stock_need_refill", "ok")}, indent=2))
    # Non-zero only when a live slot in the next 24h cannot be covered
    return 1 if short else 0


if __name__ == "__main__":
    raise SystemExit(main())
