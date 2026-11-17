"""Auto-discovery: official-only, trailer skip, logo skip, never unofficial."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import discover_links as d
import submit_link

IST = timezone(timedelta(hours=5, minutes=30))


def test_official_match_is_strict():
    official = d.official_names(ROOT)
    assert d.is_known_official("Netflix", official=official)
    assert d.is_known_official("Netflix Anime", official=official)
    assert d.is_known_official("TOHO animation", official=official)
    assert d.is_known_official("Warner Bros. Pictures", official=official)
    assert d.is_known_official("Aniplex USA", official=official)
    assert d.is_known_official("Prime Video", official=official)
    assert not d.is_known_official("Anime Recaps", official=official)
    assert not d.is_known_official("NetflixFanClips", official=official)
    assert not d.is_known_official("Best Scene Clips", official=official)
    assert not d.is_known_official("", official=official)
    # A verified-looking unknown name is still rejected
    assert not d.is_known_official("Verified Film Uploads", official=official)


def test_stamped_official_channels_are_marked_but_still_official():
    official = d.official_names(ROOT)
    assert d.is_known_official("Crunchyroll", official=official)
    assert d.is_stamped_channel("Crunchyroll")
    assert d.is_stamped_channel("Muse Asia")
    assert d.is_stamped_channel("Ani-One Asia")
    assert not d.is_stamped_channel("Netflix")
    assert not d.is_stamped_channel("TOHO animation")


def test_filter_drops_trailers_and_unofficial():
    official = {"netflix", "tohoanimation", "warnerbrospictures"}
    entries = [
        {"id": "yt:tr", "url": "https://www.youtube.com/watch?v=trailer12",
         "title": "Official Trailer", "channel": "Netflix", "handle": "", "duration": 120},
        {"id": "yt:fan", "url": "https://www.youtube.com/watch?v=fanclip99",
         "title": "Fight scene no logo", "channel": "Anime Recaps", "handle": "", "duration": 40},
        {"id": "yt:ok", "url": "https://www.youtube.com/watch?v=officialok",
         "title": "Official fight scene", "channel": "TOHO animation", "handle": "", "duration": 45},
        {"id": "yt:short", "url": "https://www.youtube.com/watch?v=tooshort1",
         "title": "Official scene", "channel": "Netflix", "handle": "", "duration": 8},
    ]
    kept = d.filter_official_candidates(entries, official=official, deny=set(), seen=set())
    urls = [c["url"] for c in kept]
    assert urls == ["https://www.youtube.com/watch?v=officialok"]
    assert not kept[0]["stamped"]


def test_logo_skips_to_next_official_never_unofficial():
    cands = [
        {"url": "https://www.youtube.com/watch?v=logoclip1", "channel": "Netflix",
         "title": "Scene 1", "id": "yt:logo", "stamped": False},
        {"url": "https://www.youtube.com/watch?v=cleanclip1", "channel": "TOHO animation",
         "title": "Scene 2", "id": "yt:clean", "stamped": False},
        {"url": "https://www.youtube.com/watch?v=fanclip01", "channel": "Anime Recaps",
         "title": "Clean fan upload", "id": "yt:fan", "stamped": False},
    ]

    def check(url):
        if "logoclip1" in url:
            return d.SampleResult(False, "logo/watermark")
        return d.SampleResult(True)

    pick, skipped = d.pick_clean_official(cands, check=check)
    assert pick is not None
    assert "cleanclip1" in pick["url"]
    assert any(s.get("skip") == "logo/watermark" for s in skipped)
    assert all("fanclip" not in (s.get("url") or "") or s.get("skip") for s in skipped)

    # Only a logo'd official clip + a clean unofficial: accept nothing
    only = [cands[0], cands[2]]
    pick2, _ = d.pick_clean_official(only, check=check)
    assert pick2 is None


def test_stamped_channel_is_skipped_for_another_official():
    cands = [
        {"url": "https://www.youtube.com/watch?v=crunchy01", "channel": "Crunchyroll",
         "title": "Official scene", "id": "yt:cr", "stamped": True},
        {"url": "https://www.youtube.com/watch?v=netflix01", "channel": "Netflix",
         "title": "Official scene", "id": "yt:nf", "stamped": False},
    ]
    calls = []

    def check(url):
        calls.append(url)
        return d.SampleResult(True)

    pick, skipped = d.pick_clean_official(cands, check=check)
    assert pick["url"].endswith("netflix01")
    assert "crunchy01" not in calls  # no need to sample a known logo-burn channel
    assert any("logo-burn" in (s.get("skip") or "") for s in skipped)


def test_discovery_queues_accepted_link(tmp_path, monkeypatch):
    monkeypatch.setenv("SKY_ROOT", str(tmp_path))
    (tmp_path / "queue").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "links").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "links" / "requests.json").write_text(json.dumps({"requests": []}))
    req = {
        "slug": "vinlandsaga",
        "words": "VINLAND SAGA",
        "hook": "Why Thorfinn dropped his daggers forever",
        "title": "Vinland Saga",
        "official_channels": ["Netflix"],
        "search_queries": ["Vinland Saga official scene clip"],
        "x_handles": [],
        "x_urls": [],
    }
    official_url = "https://www.youtube.com/watch?v=officialvs1"
    fan_url = "https://www.youtube.com/watch?v=fanuploadvs"
    logo_url = "https://www.youtube.com/watch?v=logoclipvs"

    def collect(_req):
        return d.filter_official_candidates([
            {"id": "yt:logo", "url": logo_url, "title": "Official scene",
             "channel": "Netflix", "handle": "", "duration": 50},
            {"id": "yt:fan", "url": fan_url, "title": "Official scene no logo",
             "channel": "Anime Recaps", "handle": "", "duration": 50},
            {"id": "yt:ok", "url": official_url, "title": "Official fight scene",
             "channel": "TOHO animation", "handle": "", "duration": 50},
        ], official=d.official_names(ROOT), deny=set(), seen=set())

    def check(url):
        if "logoclipvs" in url:
            return d.SampleResult(False, "logo/watermark")
        return d.SampleResult(True)

    # Pin "now" so the next free official slot is deterministic (20:30)
    now = datetime(2026, 11, 17, 16, 0, tzinfo=IST)
    monkeypatch.setattr(submit_link, "next_free_slot", lambda root, min_ahead_hours=3, **k: "2026-11-17T20:30:00+05:30")
    out = d.run_discovery(
        tmp_path, max_accept=2, dry_run=False,
        topics=[req], collect=collect, check=check,
    )
    assert out["queued_ids"] == ["20261117-2030-vinlandsaga"]
    assert official_url in out["accepted"][0]["url"]
    queued = json.loads((tmp_path / "queue" / "2026-11-17.json").read_text())
    assert queued[0]["source_url"] == official_url
    assert queued[0]["content_kind"] == "best scene"
    assert "trailer" not in queued[0]["fallback_asset"]
    reqs = json.loads((tmp_path / "links" / "requests.json").read_text())["requests"]
    assert reqs[-1]["status"] == "queued"
    assert reqs[-1]["url"] == official_url
    # second run must not queue the same official URL again
    out2 = d.run_discovery(
        tmp_path, max_accept=2, dry_run=False,
        topics=[req], collect=collect, check=check,
    )
    assert out2["queued_ids"] == []
    assert not any("fanuploadvs" in (a.get("url") or "") for a in out["accepted"])


def test_max_accept_and_dry_run(tmp_path, monkeypatch):
    (tmp_path / "queue").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "requests.json").write_text(json.dumps({"requests": []}))

    def collect(req):
        slug = req["slug"]
        return [{
            "id": f"yt:{slug}",
            "url": f"https://www.youtube.com/watch?v={slug}clip12",
            "title": "Official scene",
            "channel": "Netflix",
            "handle": "",
            "duration": 40,
            "stamped": False,
        }]

    topics = [
        {"slug": "titleone", "words": "TITLE ONE", "hook": "Why this warrior put the sword down",
         "title": "Title One", "official_channels": ["Netflix"], "search_queries": [],
         "x_handles": [], "x_urls": []},
        {"slug": "titletwo", "words": "TITLE TWO", "hook": "Why the hero walked into the fire",
         "title": "Title Two", "official_channels": ["Netflix"], "search_queries": [],
         "x_handles": [], "x_urls": []},
    ]
    out = d.run_discovery(
        tmp_path, max_accept=1, dry_run=True,
        topics=topics, collect=collect, check=lambda u: d.SampleResult(True),
    )
    assert len(out["accepted"]) == 1
    assert out["queued_ids"] == []
    assert list((tmp_path / "queue").glob("*.json")) == []
