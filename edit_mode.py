"""Edit mode for a queue item: ALWAYS music_only.

Owner rule (Nov 18 2026): no character voice or dialogue anywhere, in any
format (action, love/romance, gta6, ...). The source audio is dropped
completely and only our song plays. Legacy overrides ("dialogue_bed",
"two_act") from old queue items, SKY_EDIT_MODE or --edit-mode are mapped
to music_only so no path can bring source speech back.
"""
from __future__ import annotations

import hashlib

MODE = "music_only"


def item_mode_bucket(item_id: str) -> int:
    """Stable 0-3 bucket from the queue item id (sha1, not Python hash())."""
    return int(hashlib.sha1((item_id or "").encode("utf-8")).hexdigest()[:8], 16) % 4


def dialogue_edit_wanted(item: dict | None, env: str | None = None) -> bool:
    """Opt-in exception (BiBi, Nov 24 2026: "skywalker ko riyo ki trh"): a queue item with "dialogue_edit": true
    is rendered as a riyio-style dialogue edit (dialogue -> silence + drop -> beat-cut montage, captions).
    SKY_DIALOGUE_EDIT=1|0 overrides. Everything else stays song-only."""
    if env in ("0", "1"):
        return env == "1"
    v = (item or {}).get("dialogue_edit")
    return (v.strip().lower() in ("1", "true", "yes")) if isinstance(v, str) else bool(v)


def default_edit_mode(item_id: str, fmt: str | None = None) -> str:
    return MODE


def choose_edit_mode(item_id: str, override: str | None = None, *,
                     iconic_hook: bool = False, fmt: str | None = None) -> str:
    """Every format, every override: music_only (song only, no source audio)."""
    return MODE
