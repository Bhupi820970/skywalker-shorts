#!/usr/bin/env python3
"""Verify the next 24h of IST slots have a finished, publishable video.

Every official IST slot is checked, plus any extra slot already in the queue
(legacy 12:30/17:30 included). A slot is covered ONLY when:
  - a results/<id>.json for that slot is publishable (done + asset_url), OR
  - a ready backup-stock mp4 has been assigned to it.

Source clips on a queued item (queued+fallback) are NOT coverage — that slot
still needs a render. An unrendered backup-pool topic is also not coverage.
If short, write config/health_status.json so the workflow can dispatch the
slot render. Never deletes videos.
"""
from __future__ import annotations

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


def load_pool(root: Path) -> list[dict]:
    try:
        return json.loads((root / "config" / "backup_pool.json").read_text()).get("items") or []
    except Exception:
        return []


def result_for(root: Path, iid: str) -> dict | None:
    p = root / "results" / f"{iid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def queue_items(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / "queue").glob("2026-*.json")):
        try:
            out.extend(json.loads(Path(f).read_text()))
        except Exception:
            pass
    return out


def has_preuploaded(it: dict) -> bool:
    if it.get("fallback_asset") or it.get("fallback_mp4"):
        return True
    return False


def check(root: Path | None = None, now: datetime | None = None) -> dict:
    """Classify official slots in the next 24h. See module docstring for coverage."""
    root = Path(root or os.environ.get("SKY_ROOT") or REPO)
    now = now or datetime.now(IST)
    # Auto-retire (never delete) topics that keep failing on logo/404/bot-check.
    backup_stock.retire_broken_topics(root, now)
    # Stock whose mp4 is gone from the release is never assigned (an empty release just means "no stock").
    try:
        backup_stock.audit_lost_assets(root, now=now)
    except Exception as e:
        print("stock audit skipped:", type(e).__name__)
    horizon = now + timedelta(hours=24)
    items = queue_items(root)
    pool = load_pool(root)

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
    assigned = backup_stock.assign_needed(root, now)
    assigned_by_slot = {g["slot"]: g for g in assigned}
    stock = backup_stock.status_summary(root, now)

    short = []
    ready = []
    need_render = []
    for slot_s, group in sorted(slots.items()):
        if backup_stock.slot_is_covered(root, slot_s):
            how, iid = "done", None
            for it in group:
                r = result_for(root, it["id"])
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
            r = result_for(root, it["id"]) or {}
            if r.get("qc_rejected"):
                return "failed"
            return r.get("status")

        # Queued source clips (queued+fallback) and an unrendered backup-pool
        # topic are not coverage. The slot still needs a finished video.
        render_ids = [
            it["id"] for it in live
            if has_preuploaded(it) and _st(it) not in ("failed", "done")
        ]
        short.append(slot_s)
        need_render.extend(render_ids)

    # Extra ready target: at least 2 spare done renders beyond the next 24h live slots
    extra_needed = int(os.environ.get("EXTRA_READY_PER_DAY", "2"))
    spare_done = []
    for p in (root / "results").glob("*.json"):
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
        "need_render": need_render,
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
    return status


def main() -> int:
    status = check()
    out = (Path(os.environ.get("SKY_ROOT") or REPO) / "config" / "health_status.json")
    out.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: status[k] for k in ("slots_checked", "short", "need_render",
                                             "backup_pool_preuploaded",
                                             "stock_ready", "stock_need_refill", "ok")}, indent=2))
    # Non-zero only when a live slot in the next 24h has no finished video
    return 1 if status["short"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
