"""Clip reservation ledger (same design as pro-edit): a scene clip is reserved the moment a render picks it,
so parallel renders (render.yml matrix, max-parallel 4) can never put the same clip in two Shorts, and it is
marked used for good as soon as the Short renders (not only after it posts).

Ledger: `clip_reservations.json` on branch `clip-state` (plain JSON data, clip names only, never media).
Keys are posted_history.clip_name() values (release asset basename, or the lower-cased page URL).
  {"version": 1, "clips": {"<source id>": {"state": "reserved"|"used", "owner": "...", "platform": "...",
                                           "at": "<iso>", "video": "<video id>"}}}

* reserve(id)  -> atomic compare-and-swap through the GitHub contents API (PUT with the file sha; a 409/422
                  conflict means another run wrote first: re-read and try again). Denied if any other owner
                  holds it (reserved and not expired, or used).
* confirm(ids, video) -> state "used" (permanent: the clip is never picked again, even before it is posted).
* release(ids) -> only this owner's "reserved" entries are removed (render / QA failed).
Reservations older than RESERVE_TTL_H (a crashed run) expire so a clip is not locked forever; "used" never
expires. Locally (tests, no token) the same logic runs on a JSON file guarded by an exclusive file lock.
Tokens come from the environment (GH_TOKEN / GITHUB_TOKEN) and are never logged.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

FILE = "clip_reservations.json"
BRANCH = "clip-state"
RESERVE_TTL_H = float(os.environ.get("SKY_RESERVE_TTL_H", "8"))


_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_X = re.compile(r"(?:x|twitter)\.com/[^/]+/status/(\d+)", re.I)


def base_id(sid: str) -> str:
    """Clip key: yt:<video id> / x:<post id> for page links, else the release asset basename
    (posted_history.clip_name); query strings never collapse different videos into one key."""
    s = str(sid or "").strip()
    if s.startswith("gh:"):
        s = s[3:]
    m = _YT.search(s)
    if m:
        return f"yt:{m.group(1)}"
    m = _X.search(s)
    if m:
        return f"x:{m.group(1)}"
    low = s.lower().split("?")[0].rstrip("/")
    base = low.rsplit("/", 1)[-1]
    return base if base.endswith(".mp4") else low


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _empty() -> dict:
    return {"version": 1, "clips": {}}


class Conflict(Exception):
    pass


class LocalBackend:
    """JSON file + exclusive lock (tests / local runs)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with open(lock, "a+") as fh:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:      # pragma: no cover (non-POSIX)
                pass
            yield

    def read(self):
        try:
            return json.loads(self.path.read_text()), "local"
        except (OSError, ValueError):
            return _empty(), None

    def write(self, doc: dict, sha, msg: str):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, self.path)


class GitHubBackend:
    """Contents API on branch clip-state; the file sha makes every write a compare-and-swap."""

    def __init__(self, repo: str, token: str, branch: str = BRANCH, api: str = "https://api.github.com"):
        self.repo, self._token, self.branch, self.api = repo, token, branch, api.rstrip("/")

    @contextlib.contextmanager
    def locked(self):
        yield                      # the sha check is the lock

    def _req(self, method: str, url: str, body: dict | None = None):
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")

    def read(self):
        url = f"{self.api}/repos/{self.repo}/contents/{FILE}?ref={self.branch}"
        try:
            d = self._req("GET", url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return _empty(), None
            raise
        try:
            doc = json.loads(base64.b64decode(d.get("content") or b"").decode() or "{}")
        except ValueError:
            doc = _empty()
        return (doc if isinstance(doc, dict) else _empty()), d.get("sha")

    def write(self, doc: dict, sha, msg: str):
        body = {"message": msg, "branch": self.branch,
                "content": base64.b64encode((json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()).decode()}
        if sha:
            body["sha"] = sha
        try:
            self._req("PUT", f"{self.api}/repos/{self.repo}/contents/{FILE}", body)
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):   # someone else wrote first (sha mismatch) / file created meanwhile
                raise Conflict() from None
            raise


def default_backend():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if repo and token and os.environ.get("GITHUB_ACTIONS") == "true":
        return GitHubBackend(repo, token)
    return LocalBackend(Path(os.environ.get("SKY_RESERVE_FILE") or Path(__file__).resolve().parent / "out" / FILE))


class Reservations:
    def __init__(self, owner: str, platform: str = "", backend=None, log=print, tries: int = 8,
                 video: str = ""):
        self.owner, self.platform, self.log, self.tries = owner, platform, log, tries
        self.video = video         # a re-render of this same video may reuse its own clips
        self.backend = backend or default_backend()
        self.mine: set[str] = set()
        self.ok = True             # False after the ledger was unreachable (fail closed per clip)

    # ---- pure helpers
    @staticmethod
    def _live(e: dict, now: dt.datetime) -> bool:
        if e.get("state") == "used":
            return True
        try:
            at = dt.datetime.fromisoformat(str(e.get("at")).replace("Z", "+00:00"))
        except ValueError:
            return False
        return now - at < dt.timedelta(hours=RESERVE_TTL_H)

    def _blocked(self, e, now) -> bool:
        """Used by any video (also this run's), or reserved (not expired) by another run."""
        if not isinstance(e, dict):
            return False
        if e.get("state") == "used":
            return not (self.video and e.get("video") == self.video)
        return self._live(e, now) and e.get("owner") != self.owner

    def taken_ids(self) -> set[str]:
        """Ids that must not be picked: used by any video, or reserved by another run. Read-only."""
        try:
            doc, _ = self.backend.read()
        except Exception as ex:
            self.log("clip reservations: read failed:", type(ex).__name__)
            return set()
        now = _now()
        return {k for k, e in (doc.get("clips") or {}).items() if self._blocked(e, now)}

    def used_ids(self) -> set[str]:
        try:
            doc, _ = self.backend.read()
        except Exception:
            return set()
        return {k for k, e in (doc.get("clips") or {}).items() if isinstance(e, dict) and e.get("state") == "used"}

    def _update(self, fn, msg: str):
        """Read-modify-write with retry on conflict. fn(doc) -> result (mutates doc; None = no write)."""
        last = None
        for i in range(self.tries):
            with self.backend.locked():
                doc, sha = self.backend.read()
                doc.setdefault("version", 1)
                doc.setdefault("clips", {})
                res, changed = fn(doc)
                if not changed:
                    return res
                try:
                    self.backend.write(doc, sha, msg)
                    return res
                except Conflict as ex:
                    last = ex
            time.sleep(min(8.0, 0.5 * (2 ** i)))
        raise RuntimeError(f"clip reservations: still conflicting after {self.tries} tries") from last

    # ---- public API
    def reserve(self, sid: str) -> bool:
        b = base_id(sid)
        if not b:
            return False
        if b in self.mine:
            return True
        now = _now()

        def fn(doc):
            if self._blocked(doc["clips"].get(b), now):
                return False, False
            doc["clips"][b] = {"state": "reserved", "owner": self.owner, "platform": self.platform,
                               "at": now.isoformat(timespec="seconds")}
            return True, True
        try:
            got = self._update(fn, f"reserve clip {b} ({self.owner})")
        except Exception as ex:
            self.ok = False
            self.log("clip reservations: reserve failed, clip skipped:", b, type(ex).__name__)
            return False             # fail closed: never risk a repeat
        if got:
            self.mine.add(b)
        return got

    def confirm(self, ids, video: str) -> None:
        keep = {base_id(i) for i in ids if base_id(i)}
        if not keep:
            return
        now = _now().isoformat(timespec="seconds")

        def fn(doc):
            for b in keep:
                doc["clips"][b] = {"state": "used", "owner": self.owner, "platform": self.platform,
                                   "at": now, "video": video}
            return None, True
        self._update(fn, f"clips used by {video}")
        self.mine -= keep

    def release(self, ids=None) -> None:
        drop = {base_id(i) for i in (self.mine if ids is None else ids)} & self.mine
        if not drop:
            return

        def fn(doc):
            changed = False
            for b in drop:
                e = doc["clips"].get(b)
                if isinstance(e, dict) and e.get("owner") == self.owner and e.get("state") == "reserved":
                    del doc["clips"][b]
                    changed = True
            return None, changed
        try:
            self._update(fn, f"release {len(drop)} clip(s) ({self.owner})")
        except Exception as ex:      # they expire after RESERVE_TTL_H anyway
            self.log("clip reservations: release failed (expires by itself):", type(ex).__name__)
        self.mine -= drop


def ledger_taken(exclude_videos=(), backend=None) -> set[str]:
    """Read-only: clip keys used by any video (minus `exclude_videos`) or reserved and not expired.
    Used for stock counts and refill; best effort (renders still fail closed in reserve())."""
    try:
        doc, _ = (backend or default_backend()).read()
    except Exception:
        return set()
    now, skip, out = _now(), set(exclude_videos or ()), set()
    for k, e in (doc.get("clips") or {}).items():
        if not isinstance(e, dict):
            continue
        if e.get("state") == "used":
            if e.get("video") not in skip:
                out.add(k)
        elif Reservations._live(e, now):
            out.add(k)
    return out
