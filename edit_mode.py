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


def default_edit_mode(item_id: str, fmt: str | None = None) -> str:
    return MODE


def choose_edit_mode(item_id: str, override: str | None = None, *,
                     iconic_hook: bool = False, fmt: str | None = None) -> str:
    """Every format, every override: music_only (song only, no source audio)."""
    return MODE
