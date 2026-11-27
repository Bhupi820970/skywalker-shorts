#!/usr/bin/env python3
"""Manual QC approvals (when Gemini is down/out of quota, BIBI's bot checks the video by hand).

Data only: config/manual_qc.json is PARSED as JSON and never executed.
    {"approved": [{"id": "stock-foo-01", "approved_by": "bibi",
                   "checks": ["no_logo", "no_burned_text", "no_black_frames", "vertical_2160x3840", "audio"],
                   "at": "2026-11-27 11:30 IST"}]}

For every approved id whose render exists and is held ONLY because Gemini QC could not run
(qc_status skipped / qc_pending, gate failures all about gemini/logo "no verdict"), the result is flipped
exactly as a passed Gemini QC would: status=done, qc_pending=False, qc_status="manual_pass", gate ok.
Hard failures (failed render, QA failures, no asset, demo/test ids) are never overridden. Idempotent.
Run by health.yml (before health_check) and publish.yml (before posting).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
ID_RE = re.compile(r"^[A-Za-z0-9._@-]{1,120}$")
SOFT = ("gemini", "no verdict", "logo scan")
MIN_CHECKS = 3


def _valid(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("id"), str) and ID_RE.match(e["id"])
            and e.get("approved_by") == "bibi"
            and isinstance(e.get("checks"), list) and len(e["checks"]) >= MIN_CHECKS)


def _soft_only(r: dict) -> bool:
    g = r.get("gate") if isinstance(r.get("gate"), dict) else {}
    fails = [str(x).lower() for x in (g.get("failures") or [])]
    return all(any(s in f for s in SOFT) for f in fails)


def apply(root: Path | None = None, now: datetime | None = None) -> list[str]:
    root = Path(root or REPO)
    now = now or datetime.now(IST)
    cfg = root / "config" / "manual_qc.json"
    if not cfg.exists():
        return []
    try:
        doc = json.loads(cfg.read_text())
    except Exception:
        print("manual_qc.json unreadable; ignored")
        return []
    done = []
    for e in (doc.get("approved") or []) if isinstance(doc, dict) else []:
        if not _valid(e):
            continue
        iid = e["id"]
        if iid.startswith(("demo", "test")):
            continue
        p = root / "results" / f"{iid}.json"
        if not p.exists():
            continue
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if not r.get("asset_url") or not (r.get("qc_pending") or r.get("qc_status") == "skipped"):
            continue
        if r.get("status") not in ("retry_queued", "done") or r.get("no_publish") or r.get("demo"):
            continue
        if r.get("qa") and not all(bool(v) for v in r["qa"].values()):
            continue
        if not _soft_only(r):
            continue
        g = dict(r.get("gate") or {})
        g.update(ok=True, failures=[], gemini_qc=f"manual_pass ({e['approved_by']})")
        r.update(status="done", qc_pending=False, logo_pending=False, qc_status="manual_pass", gate=g,
                 qc_manual={"by": e["approved_by"], "checks": e["checks"], "at": e.get("at") or now.strftime("%Y-%m-%d %H:%M IST")},
                 detail="manual QC pass (Gemini unavailable)")
        r.pop("last_error", None)
        p.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
        done.append(iid)
    return done


if __name__ == "__main__":
    got = apply()
    print(f"manual_qc: approved {len(got)}: {','.join(got)}")
    sys.exit(0)
