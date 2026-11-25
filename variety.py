"""Content variety across slots (BiBi, 25 Nov): rotate categories, never repeat a franchise in a day.

  category   movie | game | cartoon | anime | tv      (topic["category"] wins, else inferred)
  franchise  dandadan2 / dandadanevil / dandadanlove -> "dandadan"; jjk0 / jujutsukaisen... -> "jjk"

Rules used by fill_queue, title_swap, backup_stock and scripts/rebalance_queue.py:
  * HARD: the same franchise never twice on one IST day.
  * soft: not within ~3 days (FRANCHISE_GAP_H), and the slot before / after is a different category.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
CATEGORIES = ("movie", "game", "cartoon", "anime", "tv")
FRANCHISE_GAP_H = 72.0
P_FRANCHISE_NEAR = 50.0   # same franchise within 3 days
P_SAME_CATEGORY = 20.0    # neighbour slot has the same category
FORBIDDEN = float("inf")

# Longest matching root wins. Explicit aliases first.
ALIASES = {
    "jujutsukaisen": "jjk", "jjk": "jjk", "princessmononoke": "mononoke", "mononoke": "mononoke",
    "thedarkknight": "batman", "thebatman": "batman", "batman": "batman",
    "dbsuperhero": "dragonball", "dragonball": "dragonball", "mha": "mha", "myheroacademia": "mha",
    "aot": "aot", "attackontitan": "aot", "spiderverse": "spiderverse", "acrossthespiderverse": "spiderverse",
    "cyberpunk": "cyberpunk", "gta": "gta", "grandtheftauto": "gta", "kgf": "kgf", "dhoom": "dhoom",
    "don": "don", "leo": "leo", "fnaf": "fnaf", "fivenights": "fnaf", "missionimpossible": "missionimpossible",
    "johnwick": "johnwick", "demonslayer": "demonslayer", "kimetsu": "demonslayer", "frieren": "frieren",
    "dandadan": "dandadan", "arcane": "arcane", "apothecarydiaries": "apothecarydiaries",
    "spyxfamily": "spyxfamily", "bluelock": "bluelock", "kaiju8": "kaiju8", "onepiece": "onepiece",
    "vinlandsaga": "vinlandsaga", "haikyu": "haikyu", "bleach": "bleach", "eldenring": "eldenring",
    "godofwar": "godofwar", "zootopia": "zootopia", "dune": "dune", "deadpool": "deadpool",
    "venom": "venom", "gladiator": "gladiator", "jurassic": "jurassic", "sololeveling": "sololeveling",
    "chainsawman": "chainsawman", "mobpsycho": "mobpsycho", "rezero": "rezero", "oshinoko": "oshinoko",
    "mushokutensei": "mushokutensei", "spiderman": "spiderman", "strangerthings": "strangerthings",
    "squidgame": "squidgame", "kungfupanda": "kungfupanda", "despicableme": "despicableme",
    "minions": "despicableme", "gru": "despicableme", "toystory": "toystory", "insideout": "insideout",
    "weatheringwithyou": "weatheringwithyou", "yourname": "yourname", "suzume": "suzume",
}

_SHORT_ANY = {"jjk", "mha", "aot", "gta"}  # short roots that may be followed by anything (jjkgojovstoji)

GAME = ("gta", "grandtheftauto", "cyberpunk2077", "eldenring", "godofwar", "ghostoftsushima", "ghostofyotei",
        "reddeadredemption", "rdr2", "fortnite", "callofduty", "valorant", "leagueoflegendsgame", "minecraft",
        "residentevilgame", "spidermangame", "marvelsspiderman", "deathstranding", "assassinscreed",
        "tekken", "streetfighter", "blackmythwukong", "hollowknight", "halo", "forzahorizon")
CARTOON = ("spiderverse", "acrossthespiderverse", "arcane", "zootopia", "insideout", "kungfupanda", "shrek",
           "despicableme", "minions", "gru", "toystory", "moana", "frozen", "incredibles", "coco", "encanto",
           "tmnt", "castlevania", "invincible", "avatarthelastairbender", "lastairbender", "hoppers",
           "wildrobot", "puss", "elemental", "turningred", "luca", "soul", "megamind", "madagascar")
TV = ("strangerthings", "breakingbad", "squidgame", "wednesday", "gameofthrones", "houseofthedragon",
      "mirzapur", "familyman", "panchayat", "sacredgames", "thebear", "peakyblinders", "moneyheist",
      "thelastofus", "succession", "loki", "andor", "mandalorian", "daredevil", "severance", "fallout",
      "shogun", "dark", "theoffice", "friends", "kota", "scam1992", "paatallok", "aspirants")
_GAME_HINT = re.compile(r"\b(gameplay|video ?game|ps5|playstation|xbox|rockstar|fromsoftware|cd projekt)\b", re.I)
_TV_HINT = re.compile(r"\b(web ?series|tv series|netflix series|season \d+ episode|hbo series)\b", re.I)


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def slug_of(item: dict | None) -> str:
    """Footage slug of a queue item / result / catalog topic (after any title swap)."""
    it = item or {}
    s = it.get("swapped_to") or it.get("slug") or ""
    if not s:
        iid = str(it.get("id") or "")
        parts = iid.split("-")
        if len(parts) >= 3 and parts[0].isdigit():
            s = "-".join(parts[2:])
        elif iid.startswith("backup-"):
            s = iid[len("backup-"):]
        elif iid.startswith("stock-"):
            s = re.sub(r"-\d+$", "", iid[len("stock-"):])
        else:
            s = iid
    return _norm(s)


def franchise(x) -> str:
    s = _norm(slug_of(x) if isinstance(x, dict) else x)
    if not s:
        return ""
    best = ""
    for root in ALIASES:
        if s.startswith(root) and len(root) > len(best):
            best = root
    if best and (len(best) >= 4 or best in _SHORT_ANY or s == best
                 or re.fullmatch(best + r"(\d+|s\d+|movie|final)?", s)):
        return ALIASES[best]
    s = re.sub(r"(s\d+|\d+|movie|final|season\d*|part\d*)$", "", s) or s
    return s


def category(topic: dict | None, catalog: dict | None = None) -> str:
    t = topic or {}
    c = str(t.get("category") or "").lower()
    if c in CATEGORIES:
        return c
    slug = slug_of(t)
    if catalog and slug in catalog:
        c = str(catalog[slug].get("category") or "").lower()
        if c in CATEGORIES:
            return c
        t = {**catalog[slug], **{k: v for k, v in t.items() if v}}
    blob = " ".join([slug, str(t.get("words") or ""), str(t.get("content_kind") or ""),
                     " ".join(str(x) for x in t.get("tags") or [])])
    if any(slug.startswith(g) for g in GAME) or t.get("edit_format") == "gta6" or \
            str(t.get("content_kind") or "") == "gameplay" or _GAME_HINT.search(blob):
        return "game"
    if any(slug.startswith(k) for k in CARTOON):
        return "cartoon"
    if any(slug.startswith(k) for k in TV) or _TV_HINT.search(blob):
        return "tv"
    pillar = str(t.get("pillar") or "").lower()
    if pillar == "anime" or re.search(r"\banime\b", blob, re.I):
        return "anime"
    if pillar in ("tv", "series", "webseries"):
        return "tv"
    if pillar in ("game", "gaming"):
        return "game"
    if pillar in ("cartoon", "animation"):
        return "cartoon"
    if pillar == "romance":  # love edits: anime unless the catalog says otherwise
        return "anime" if not re.search(r"\b(bollywood|hollywood)\b", blob, re.I) else "movie"
    return "movie"


def load_catalog(root: Path) -> dict:
    """slug -> topic (topic_catalog first, then backup_pool topics as backup-<slug>)."""
    out: dict = {}
    try:
        items = json.loads((Path(root) / "config" / "topic_catalog.json").read_text()).get("items") or []
    except Exception:
        items = []
    for t in items:
        if t.get("slug"):
            out[_norm(t.get("slug"))] = t
    try:
        pool = json.loads((Path(root) / "config" / "backup_pool.json").read_text()).get("items") or []
    except Exception:
        pool = []
    for b in pool:
        s = slug_of(b)
        if s and s not in out:
            out[s] = {**b, "slug": s}
    return out


def parse_slot(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except Exception:
        return None


def slot_of(item: dict) -> datetime | None:
    dt = parse_slot(item.get("slot") or "")
    if dt:
        return dt
    m = re.match(r"^(\d{8})-(\d{4})-", str(item.get("id") or ""))
    if m:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(tzinfo=IST)
    return None


def context(root: Path, around: datetime, *, days: int = 4, exclude_ids: set[str] | None = None,
            catalog: dict | None = None) -> list[dict]:
    """Every live queue slot within +-days: {"id", "dt", "franchise", "category"} (result footage wins)."""
    root = Path(root)
    catalog = catalog if catalog is not None else load_catalog(root)
    exclude_ids = exclude_ids or set()
    out = []
    for i in range(-days, days + 1):
        day = (around.astimezone(IST) + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            items = json.loads((root / "queue" / f"{day}.json").read_text())
        except Exception:
            continue
        for it in items:
            if not isinstance(it, dict) or it.get("status") in ("replaced", "retired") or it.get("demo"):
                continue
            iid = str(it.get("id") or "")
            if iid in exclude_ids:
                continue
            src = it
            try:
                r = json.loads((root / "results" / f"{iid}.json").read_text())
                if r.get("swapped_to") or r.get("assigned_from_stock"):
                    src = {**it, "swapped_to": r.get("swapped_to") or r.get("assigned_from_stock") or ""}
            except Exception:
                pass
            dt = slot_of(it)
            if dt is None:
                continue
            out.append({"id": iid, "dt": dt, "franchise": franchise(src), "category": category(src, catalog)})
    return out


def penalty(fr: str, cat: str, dt: datetime, ctx: list[dict]) -> float:
    """0 = perfect. FORBIDDEN when the franchise already runs that IST day."""
    if dt is None:
        return 0.0
    day = dt.astimezone(IST).date()
    p = 0.0
    before = [c for c in ctx if c["dt"] < dt]
    after = [c for c in ctx if c["dt"] > dt]
    for c in ctx:
        if fr and c["franchise"] == fr:
            if c["dt"].astimezone(IST).date() == day:
                return FORBIDDEN
            if abs((c["dt"] - dt).total_seconds()) < FRANCHISE_GAP_H * 3600:
                p += P_FRANCHISE_NEAR
    prev = max(before, key=lambda c: c["dt"]) if before else None
    nxt = min(after, key=lambda c: c["dt"]) if after else None
    for nb in (prev, nxt):
        if nb and nb["category"] == cat and abs((nb["dt"] - dt).total_seconds()) < 6 * 3600:
            p += P_SAME_CATEGORY
    return p


def topic_penalty(topic: dict, dt: datetime | None, ctx: list[dict], catalog: dict | None = None) -> float:
    return penalty(franchise(topic), category(topic, catalog), dt, ctx) if dt else 0.0


def tag_catalogs(root: Path) -> dict:
    """Write an explicit category onto every topic_catalog / backup_pool topic (idempotent)."""
    root = Path(root)
    counts: dict[str, int] = {}
    cat_map = load_catalog(root)
    for rel in ("config/topic_catalog.json", "config/backup_pool.json"):
        p = root / rel
        try:
            doc = json.loads(p.read_text())
        except Exception:
            continue
        changed = False
        for t in doc.get("items") or []:
            c = category(t, cat_map if rel.endswith("backup_pool.json") else None)
            if t.get("category") != c:
                t["category"] = c
                changed = True
            if rel.endswith("topic_catalog.json"):
                counts[c] = counts.get(c, 0) + 1
        if changed:
            p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return counts


if __name__ == "__main__":
    import sys
    print(tag_catalogs(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent))
