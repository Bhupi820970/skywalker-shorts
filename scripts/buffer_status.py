#!/usr/bin/env python3
"""Read-only: print recent Buffer posts for the Skywalker channel and the daily posting limit.

Env: BUFFER_API_KEY (secret; never printed). Optional DELETE_IDS=comma list of queued
(scheduled/error/draft) post ids to delete; sent posts are never touched.
"""
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buffer_post as bp  # noqa: E402


def main():
    posts = bp.channel_posts()
    posts.sort(key=lambda x: x.get("dueAt") or x.get("sentAt") or "", reverse=True)
    for x in posts[:25]:
        t = ((x.get("metadata") or {}).get("title") or "")[:80]
        err = ((x.get("error") or {}).get("message") or "")[:120]
        print(f"{x['id']} {x.get('status'):10} due={x.get('dueAt')} sent={x.get('sentAt')} {t!r} {err}")
    now = datetime.now(timezone.utc)
    for h in range(0, 30, 3):
        t = now + timedelta(hours=h)
        try:
            st = bp.limit_status(t)
        except Exception as e:
            print("dailyPostingLimits error:", e)
            break
        print(f"limit @ {t.astimezone(bp.IST):%Y-%m-%d %H:%M IST}: {st}")
    dels = [x.strip() for x in os.environ.get("DELETE_IDS", "").split(",") if x.strip()]
    byid = {x["id"]: x for x in posts}
    for pid in dels:
        x = byid.get(pid)
        if not x:
            print(f"delete {pid}: not found on channel; skipped")
            continue
        if x.get("status") == "sent":
            print(f"delete {pid}: already sent; NOT deleting (never delete published videos)")
            continue
        print(f"delete {pid}: {bp.delete_post(pid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
