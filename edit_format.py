"""Load edit-format templates (love, GTA 6) and helpers for music, pacing, hooks, quotas."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config" / "edit_formats.json"

LOVE_KEYWORDS = (
    "love", "romance", "romantic", "kiss", "couple", "heart", "sad",
    "emotional", "longing", "almost kiss", "first sight", "wedding",
    "confession", "crush", "soulmate", "together",
)
ACTION_KEYWORDS = (
    "fight", "battle", "war", "punch", "aura", "goes all out", "killer",
    "monster", "beast", "rage", "revenge", "gameplay",
)
_DENY_CACHE: list[str] | None = None


def load_formats(root: Path | None = None) -> dict:
    path = (Path(root) if root else ROOT) / "config" / "edit_formats.json"
    if not path.exists():
        path = CFG_PATH
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def love_cfg(root: Path | None = None) -> dict:
    return dict((load_formats(root).get("love") or {}))


def gta6_cfg(root: Path | None = None) -> dict:
    return dict((load_formats(root).get("gta6") or {}))


def _blob(item: dict | None, extra: str = "") -> str:
    item = item or {}
    parts = [
        item.get("pillar") or "",
        item.get("edit_format") or "",
        item.get("content_kind") or "",
        item.get("hook") or "",
        item.get("title") or "",
        item.get("words") or "",
        item.get("slug") or "",
        " ".join(item.get("tags") or []),
        extra or "",
    ]
    return " ".join(str(p) for p in parts).lower()


def _love_title_names(root: Path | None = None) -> list[str]:
    names = []
    for r in (love_cfg(root).get("titles") or []):
        if isinstance(r, dict):
            for k in ("words", "title", "slug"):
                if r.get(k):
                    names.append(str(r[k]).lower())
        elif r:
            names.append(str(r).lower())
    return names



# Romantic/cute clips only. Titles matching these are never queued or discovered.
_EXPLICIT_RE = re.compile(
    r"\b("
    r"sex(?:ual)?(?:\s+scene)?|nsfw|nude|naked|nudity|porn(?:o|ographic)?|xxx|"
    r"hentai|ecchi|explicit|uncensored|18\+|r[\s-]?rated\s+sex|onlyfans|"
    r"rule\s*34|r34|intercourse|orgasm|erotic|strip(?:ping|tease)?|"
    r"making\s+love|make\s+love|sex\s*tape|fanservice|fan\s*service|"
    r"lingerie\s+scene|bedroom\s+sex"
    r")\b",
    re.I,
)


def is_explicit_text(*parts: object) -> bool:
    """True when title/hook/description looks sexual or explicit (not just romantic)."""
    blob = " ".join(str(p or "") for p in parts)
    if not blob.strip():
        return False
    return bool(_EXPLICIT_RE.search(blob))


def is_explicit_item(item: dict | None = None, extra: str = "") -> bool:
    item = item or {}
    return is_explicit_text(
        item.get("title"), item.get("hook"), item.get("words"),
        item.get("content_kind"), item.get("description"), extra,
        " ".join(item.get("search_queries") or []),
        " ".join(item.get("tags") or []),
    )


def is_love_item(item: dict | None = None, *, pillar: str = "", text: str = "") -> bool:
    """True when a queue item or hook/title is a romance/love edit."""
    p = (pillar or (item or {}).get("pillar") or "").lower()
    if p in ("romance", "love"):
        return True
    if (item or {}).get("edit_format") in ("love", "romance"):
        return True
    kind = ((item or {}).get("content_kind") or "").lower()
    if any(k in kind for k in ("romance", "love scene", "romantic")):
        return True
    blob = _blob(item, text)
    compact = re.sub(r"[^a-z0-9]+", "", blob)
    for name in _love_title_names():
        tok = re.sub(r"[^a-z0-9]+", "", name)
        if len(tok) >= 5 and tok in compact:
            return True
    if any(w in blob for w in ACTION_KEYWORDS) and not any(w in blob for w in LOVE_KEYWORDS):
        return False
    return any(w in blob for w in LOVE_KEYWORDS)


def is_gta6_item(item: dict | None = None, text: str = "") -> bool:
    blob = _blob(item, text)
    compact = re.sub(r"[^a-z0-9]+", "", blob)
    return any(s in compact for s in ("gta6", "gtavi", "grandtheftautovi", "grandtheftauto6"))


def infer_format(item: dict | None = None, *, pillar: str = "", text: str = "") -> str:
    if is_gta6_item(item, text):
        return "gta6"
    if is_love_item(item, pillar=pillar, text=text):
        return "love"
    return "action"


def pacing_for(fmt: str, root: Path | None = None) -> dict:
    """Shot-length overrides for a format. Empty dict means keep defaults."""
    if fmt != "love":
        return {}
    p = dict((love_cfg(root).get("pacing") or {}))
    return {
        "min_shot_len": float(p.get("min_shot_len") or 2.5),
        "hero_shot_min": float(p.get("hero_shot_min") or 3.0),
        "hero_shot_max": float(p.get("hero_shot_max") or 5.0),
        "max_flash_shots": int(p.get("max_flash_shots") or 0),
        "max_hero_shots": int(p.get("max_hero_shots") or 4),
        "target_dur_min": float(p.get("target_dur_min") or 20),
        "target_dur_max": float(p.get("target_dur_max") or 32),
    }


def caption_style(fmt: str, root: Path | None = None) -> dict:
    if fmt != "love":
        return {"style": "lower_third", "font_name": "Poppins", "font_file": "Poppins-BoldItalic.ttf",
                "keyword_highlight": True, "letter_spacing": 1.0, "position": "lower_third"}
    c = dict((love_cfg(root).get("captions") or {}))
    c.setdefault("style", "cinematic_center")
    c.setdefault("font_name", "Liberation Serif")
    c.setdefault("font_file", "LiberationSerif-Bold.ttf")
    c.setdefault("keyword_highlight", False)
    c.setdefault("letter_spacing", 2.5)
    c.setdefault("position", "plate_lower_center")
    return c


def hook_banned_phrases(root: Path | None = None) -> list[str]:
    global _DENY_CACHE
    if _DENY_CACHE is not None and root is None:
        return _DENY_CACHE
    phrases = [str(x).strip().lower() for x in (love_cfg(root).get("hook") or {}).get("never_use") or [] if x]
    if root is None:
        _DENY_CACHE = phrases
    return phrases


def hook_uses_banned_phrase(text: str, root: Path | None = None) -> bool:
    blob = re.sub(r"\s+", " ", (text or "").lower())
    return any(p and p in blob for p in hook_banned_phrases(root))


def hook_prompt_addon(fmt: str, root: Path | None = None) -> str:
    """Extra Gemini instructions for a format. Never includes reference Shorts wording."""
    if fmt != "love":
        return ""
    h = love_cfg(root).get("hook") or {}
    examples = ", ".join(f'"{x}"' for x in (h.get("style_examples") or [])[:5])
    banned = "; ".join(hook_banned_phrases(root)[:8])
    return (
        "This is a LOVE/romance edit, not an action edit. Write a 3-7 word PURE ENGLISH hook "
        "that is a short emotional observation about the moment (often past tense or a quiet feeling). "
        "It must be specific to these characters and shots. Do not write fight, aura, or hype hooks. "
        "No emoji, no hashtags. Style examples (do not copy verbatim, invent a new line): "
        f"{examples}. Never use these phrases: {banned}."
    )


def gta6_launch_at(root: Path | None = None) -> datetime:
    raw = (gta6_cfg(root).get("launch_at") or "2026-11-19T00:00:00+00:00")
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def gta6_has_launched(now: datetime | None = None, root: Path | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= gta6_launch_at(root)


def daily_quotas(now: datetime | None = None, root: Path | None = None) -> dict:
    """How many love / GTA 6 items to try to schedule today."""
    love = love_cfg(root).get("schedule") or {}
    gta = gta6_cfg(root).get("schedule") or {}
    launched = gta6_has_launched(now, root)
    return {
        "love_min": int(love.get("min_per_day") or 2),
        "love_max": int(love.get("max_per_day") or 3),
        "gta6_min": int(gta.get("min_per_day_after_launch") or 2) if launched else 0,
        "gta6_max": int(gta.get("max_per_day_after_launch") or 3) if launched else 0,
        "gta6_launched": launched,
    }


def trending_score(text: str, trends: dict | None = None, titles: list[str] | None = None) -> float:
    """Small boost when a title matches currently trending names/phrases."""
    blob = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    if not blob.strip():
        return 0.0
    score = 0.0
    names: list[str] = []
    for src in (titles or []):
        if src:
            names.append(str(src))
    tr = trends or {}
    for key in ("titles", "picks", "topics", "trending_titles", "requests"):
        for row in tr.get(key) or []:
            if isinstance(row, str):
                names.append(row)
            elif isinstance(row, dict):
                names.append(str(row.get("words") or row.get("title") or row.get("slug") or ""))
    for n in names:
        tok = re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()
        if len(tok) >= 4 and tok in blob:
            score += 6.0
        elif len(tok) >= 4 and any(w and len(w) >= 5 and w in blob for w in tok.split()):
            score += 2.0
    return score


def load_trending_titles(root: Path | None = None) -> list[str]:
    root = Path(root) if root else ROOT
    out: list[str] = []
    for rel in ("trends/latest.json", "config/clip_requests.json", "stats/latest.json"):
        p = root / rel
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for key in ("titles", "picks", "topics", "trending_titles", "requests", "items"):
            for row in doc.get(key) or []:
                if isinstance(row, str) and row.strip():
                    out.append(row.strip())
                elif isinstance(row, dict):
                    name = row.get("words") or row.get("title") or row.get("slug") or row.get("name")
                    if name:
                        out.append(str(name).strip())
    # de-dupe, keep order
    seen, uniq = set(), []
    for n in out:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(n)
    return uniq[:80]
