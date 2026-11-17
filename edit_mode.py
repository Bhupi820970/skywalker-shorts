"""Pick music_only vs two_act vs dialogue_bed for a queue item.

Non-love (action) Shorts are always song-only: no leftover character
dialogue, SFX, or dialogue captions. Romance/love edits keep the original
speech and duck the instrumental (dialogue_bed). Two-act is only used
when an explicit override is set (queue edit_mode / SKY_EDIT_MODE /
--edit-mode). An explicit override always wins.
"""
from __future__ import annotations

import hashlib
import os


def item_mode_bucket(item_id: str) -> int:
    """Stable 0-3 bucket from the queue item id (sha1, not Python hash())."""
    return int(hashlib.sha1((item_id or "").encode("utf-8")).hexdigest()[:8], 16) % 4


def default_edit_mode(item_id: str, fmt: str | None = None) -> str:
    """Action/non-love is always music_only. Love defaults to dialogue_bed."""
    if (fmt or "").strip().lower() in ("love", "romance"):
        return "dialogue_bed"
    return "music_only"


def choose_edit_mode(item_id: str, override: str | None = None, *,
                     iconic_hook: bool = False, fmt: str | None = None) -> str:
    """Pick music_only, two_act, or dialogue_bed. An explicit override always wins.

    Romance/love edits always keep the original dialogue and duck the
    instrumental under speech (dialogue_bed), unless an override is set.
    Non-love edits stay music_only even when Gemini flags an iconic hook,
    so leftover trailer dialogue cannot leak into the cut.
    """
    ov = (override or os.environ.get("SKY_EDIT_MODE") or "").strip().lower()
    if ov in ("music_only", "two_act", "dialogue_bed"):
        return ov
    if (fmt or "").strip().lower() in ("love", "romance"):
        return "dialogue_bed"
    # iconic_hook is ignored for action: leftover dialogue failed Lazarus QC.
    return "music_only"
