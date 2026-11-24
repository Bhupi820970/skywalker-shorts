"""Tiny Gemini REST client for the Skywalker pipeline (stdlib only).

- Keys come from env GEMINI_API_KEY and GEMINI_API_KEY_2..GEMINI_API_KEY_7
  (GitHub secrets). Values are never printed, logged, or written to the budget file.
- On 429 / quota / 503 for a key, try the next model, then the next key.
  The last key (and model) that worked is tried first on the next call.
  Missing or empty keys are skipped.
- On 429 the key cools ~60s (next key, same model); on 503 the model cools ~40s for
  every key (next model). When everything is cooling the call waits it out (backoff).
- Per-video budget, tracked in a small JSON file (SKY_GEMINI_BUDGET_FILE) shared by every
  attempt/process of one render, split into pools so a logo-scan storm can never starve
  the creative calls or QC: main (GEMINI_MAX_CALLS 5 / GEMINI_MAX_HTTP 16), logo
  (GEMINI_LOGO_MAX_*), qc (GEMINI_QC_MAX_*); one call is capped at GEMINI_CALL_MAX_HTTP (10).
- Every helper returns None on any failure; callers fall back to heuristics.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-3.1-flash-lite"]
KEY_ENVS = ("GEMINI_API_KEY",) + tuple(f"GEMINI_API_KEY_{i}" for i in range(2, 8))


def _models() -> list[str]:
    env = (os.environ.get("GEMINI_MODELS") or "").strip()
    return [m.strip() for m in env.split(",") if m.strip()] or list(DEFAULT_MODELS)


def _key_slot(name: str) -> str:
    """Log label only (key1, key2, ...); never the secret value."""
    if name == "GEMINI_API_KEY":
        return "key1"
    return "key" + name.rsplit("_", 1)[-1]


def _keys() -> list[tuple[str, str]]:
    """Non-empty keys as (env_name, value), in slot order. Callers must not log the value."""
    out = []
    for name in KEY_ENVS:
        v = (os.environ.get(name) or "").strip()
        if v:
            out.append((name, v))
    return out


def available() -> bool:
    return bool(_keys()) and os.environ.get("SKY_GEMINI", "1") != "0"


def _budget_path() -> Path | None:
    p = os.environ.get("SKY_GEMINI_BUDGET_FILE")
    return Path(p) if p else None


def _budget() -> dict:
    p = _budget_path()
    if p and p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"calls": 0, "http": 0, "log": []}


def _save_budget(b: dict) -> None:
    p = _budget_path()
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(b, indent=1))


def budget_left() -> int:
    mx = int(os.environ.get("GEMINI_MAX_CALLS", "5"))
    return max(0, mx - int(_budget().get("calls", 0)))


def _key_env_from_label(label: str | None) -> str | None:
    """Map a log label (key1, key2, ...) or env name to an env name."""
    if not label:
        return None
    if label in KEY_ENVS:
        return label
    if label == "key1":
        return "GEMINI_API_KEY"
    if label.startswith("key") and label[3:].isdigit():
        return f"GEMINI_API_KEY_{label[3:]}"
    return None


def start_after_key(label: str | None) -> None:
    """On the next call, try the key after `label` first (rotation after a skip/429)."""
    env = _key_env_from_label(label)
    if not env:
        return
    names = [n for n, _ in _keys()]
    if env not in names:
        return
    nxt = names[(names.index(env) + 1) % len(names)]
    b = _budget()
    b["good_key"] = nxt
    b["start_after_key"] = env
    _save_budget(b)


def allow_qc_retry(after_key: str | None = None) -> None:
    """Reserve one extra QC call and rotate to the next Gemini key."""
    b = _budget()
    b["qc_retry"] = int(b.get("qc_retry") or 0) + 1
    _save_budget(b)
    if after_key:
        start_after_key(after_key)


def last_key_from_log(log: list | None) -> str | None:
    for ev in reversed(log or []):
        if ev.get("key"):
            return ev["key"]
    return None


def _ordered_keys() -> list[tuple[str, str]]:
    keys = _keys()
    b = _budget()
    start_after = _key_env_from_label(b.get("start_after_key"))
    if start_after and any(n == start_after for n, _ in keys):
        names = [n for n, _ in keys]
        i = names.index(start_after)
        keys = keys[i + 1:] + keys[: i + 1]
        b["start_after_key"] = None  # consume once
        _save_budget(b)
        return keys
    good = b.get("good_key")
    if good and any(n == good for n, _ in keys):
        keys = [(n, k) for n, k in keys if n == good] + [(n, k) for n, k in keys if n != good]
    return keys


def _ordered_models() -> list[str]:
    models = _models()
    good = _budget().get("good_model")
    if good in models:
        models = [good] + [m for m in models if m != good]
    return models


def _quota_error(code: int, body: bytes | str | None = None) -> bool:
    if code in (429, 503):
        return True
    if body and code in (403,):
        text = body.decode("utf-8", "ignore") if isinstance(body, (bytes, bytearray)) else str(body)
        low = text.lower()
        return "quota" in low or "resource_exhausted" in low or "rate limit" in low or "rate-limit" in low
    return False


def _img_part(path: str | Path) -> dict:
    data = Path(path).read_bytes()
    low = str(path).lower()
    mime = ("image/png" if low.endswith(".png") else "video/mp4" if low.endswith(".mp4")
            else "audio/mpeg" if low.endswith(".mp3") else "image/jpeg")
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}


def _post(model: str, key: str, body: bytes, timeout: float):
    """POST generateContent. Returns ('ok', data) | ('http', code, raw) | ('err', typ)."""
    req = urllib.request.Request(
        API.format(m=model), data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok", json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:
            pass
        return "http", e.code, raw
    except Exception as e:
        return "err", type(e).__name__


def _text_from_response(d: dict) -> str:
    return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()


# Budget pools. The dense logo scan and the QC verdict are hard safety gates, so they
# get their OWN counters: a 429/503 storm during the logo scan can no longer eat the
# creative budget (scene pick / hook / ref-match plan) or the QC call. Run
# 45429384201 burned 81 HTTP requests on logo_scenes, so every later call said
# "http budget exhausted" and the render shipped without Gemini planning or QC.
POOLS = {
    # pool: (calls env, calls default, http env, http default)
    "main": ("GEMINI_MAX_CALLS", 5, "GEMINI_MAX_HTTP", 16),
    "logo": ("GEMINI_LOGO_MAX_CALLS", 30, "GEMINI_LOGO_MAX_HTTP", 90),
    "qc": ("GEMINI_QC_MAX_CALLS", 4, "GEMINI_QC_MAX_HTTP", 24),
}
COOL_KEY_S = 60.0     # 429 on a key: per-minute quota, rest that key ~1 min
COOL_MODEL_S = 40.0   # 503 on a model: server overload is model-wide, not per key
COOL_PAIR_S = 45.0    # timeout / bad JSON on one key+model


def _pool(purpose: str) -> str:
    p = purpose or ""
    if p.startswith("logo"):
        return "logo"
    if p == "qc":
        return "qc"
    return "main"


def _pool_counters(pool: str) -> tuple[str, str]:
    return ("calls", "http") if pool == "main" else (f"{pool}_calls", f"{pool}_http")


def _pool_caps(pool: str) -> tuple[int, int]:
    ce, cd, he, hd = POOLS[pool]
    return int(os.environ.get(ce, str(cd))), int(os.environ.get(he, str(hd)))


def _cool(b: dict, kind: str, name: str, secs: float) -> None:
    m = b.setdefault(kind, {})
    m[name] = max(float(m.get(name) or 0), time.time() + secs)


def _cooling(b: dict, kind: str, name: str, now: float | None = None) -> float:
    """Seconds left on a cooldown (0 if free)."""
    now = time.time() if now is None else now
    return max(0.0, float((b.get(kind) or {}).get(name) or 0) - now)


def _attempt_order(b: dict) -> list[tuple[str, str, str]]:
    """(key_env, key_value, model) pairs to try: last-good first, cooled keys/models last.

    429 cools a KEY (move to the next key, same good model); 503 cools a MODEL for every key
    (move to the next model instead of hammering the overloaded one on all 7 keys)."""
    now = time.time()
    keys = _ordered_keys()
    models = _ordered_models()
    pairs = []
    for mi, model in enumerate(models):
        for ki, (name, key) in enumerate(keys):
            wait = max(_cooling(b, "cool_keys", name, now), _cooling(b, "cool_models", model, now),
                       _cooling(b, "cool_pairs", f"{name}|{model}", now))
            # free pairs first; within free: walk keys on the best model before dropping to a worse model
            pairs.append((wait > 0, wait, mi, ki, name, key, model))
    pairs.sort(key=lambda p: (p[0], p[1] if p[0] else 0, p[2], p[3]))
    return [(p[4], p[5], p[6]) for p in pairs]


def _log(purpose: str, model: str, slot: str, **kw) -> None:
    b = _budget()
    b.setdefault("log", []).append({"purpose": purpose, "model": model, "key": slot, **kw})
    _save_budget(b)


def generate_json(prompt: str, images: list | None = None, *, purpose: str = "",
                  temperature: float = 0.6, timeout: float = 90.0) -> dict | list | None:
    """One logical call. Returns parsed JSON or None (never raises).

    Budgets are per pool (main / logo / qc). One logical call is also capped
    (GEMINI_CALL_MAX_HTTP, default 10) so a single 429/503 storm cannot drain the pool.
    """
    if not available():
        return None
    pool = _pool(purpose)
    ck, hk = _pool_counters(pool)
    mx_calls, mx_http = _pool_caps(pool)
    per_call = int(os.environ.get("GEMINI_CALL_MAX_HTTP", "10"))
    b = _budget()
    if b.get(ck, 0) >= mx_calls:
        if purpose == "qc" and int(b.get("qc_retry") or 0) > 0:
            b["qc_retry"] = int(b.get("qc_retry") or 0) - 1
            _save_budget(b)
            print(f"  gemini[qc]: reserved retry after budget; rotating key")
        else:
            print(f"  gemini[{purpose}]: budget exhausted ({mx_calls} {pool} calls); using heuristics")
            return None
    parts = [{"text": prompt}] + [_img_part(p) for p in (images or []) if p and Path(p).exists()]
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
    }).encode()
    b[ck] = b.get(ck, 0) + 1
    _save_budget(b)
    deadline = time.time() + float(os.environ.get("GEMINI_DEADLINE_S", "300"))
    sent = 0
    dead_keys: set[str] = set()   # auth failures: never retried in this call
    gone_models: set[str] = set()  # 404: model not served
    tried: set[tuple[str, str]] = set()
    while sent < per_call:
        b = _budget()
        order = [(n, k, m) for n, k, m in _attempt_order(b)
                 if n not in dead_keys and m not in gone_models]
        if not order:
            print(f"  gemini[{purpose}]: no key/model left; using heuristics")
            return None
        # Prefer a pair not tried yet in this call; once all are tried, re-use the
        # one whose cooldown ends first (after waiting it out = real backoff).
        fresh = [x for x in order if (x[0], x[2]) not in tried]
        name, key, model = (fresh or order)[0]
        wait = max(_cooling(b, "cool_keys", name), _cooling(b, "cool_models", model),
                   _cooling(b, "cool_pairs", f"{name}|{model}"))
        if wait > 0:
            if time.time() + wait > deadline:
                print(f"  gemini[{purpose}]: every key/model cooling down past the deadline; using heuristics")
                return None
            print(f"  gemini[{purpose}]: all keys/models cooling; backoff {wait:.0f}s")
            time.sleep(min(wait, 60.0))
        if time.time() > deadline:
            print(f"  gemini[{purpose}]: deadline reached; using heuristics")
            return None
        b = _budget()
        if b.get(hk, 0) >= mx_http:
            print(f"  gemini[{purpose}]: {pool} http budget exhausted")
            return None
        b[hk] = b.get(hk, 0) + 1
        _save_budget(b)
        sent += 1
        tried.add((name, model))
        slot = _key_slot(name)
        t0 = time.time()
        result = _post(model, key, body, timeout)
        kind = result[0]
        if kind == "ok":
            try:
                txt = _text_from_response(result[1])
                if txt.startswith("```"):
                    txt = txt.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
                out = json.loads(txt)
            except Exception as e:
                kind, result = "err", ("err", type(e).__name__)
            else:
                b = _budget()
                b["good_model"] = model
                b["good_key"] = name  # env name only, never the secret
                _save_budget(b)
                _log(purpose, model, slot, s=round(time.time() - t0, 1), ok=True)
                print(f"  gemini[{purpose}]: ok via {slot}/{model} in {time.time() - t0:.1f}s")
                return out
        if kind == "http":
            code, raw = result[1], result[2]
            msg = f"HTTP {code}"
            _log(purpose, model, slot, err=msg)
            if code == 400:
                print(f"  gemini[{purpose}]: {msg} (request); giving up")
                return None
            b = _budget()
            if code in (401, 403) and not _quota_error(code, raw):
                print(f"  gemini[{purpose}]: {slot} {msg} (auth); next key")
                dead_keys.add(name)
                continue
            if code == 404:
                gone_models.add(model)
                continue
            if code == 503:
                _cool(b, "cool_models", model, COOL_MODEL_S)
                print(f"  gemini[{purpose}]: {slot}/{model} overloaded ({msg}); next model")
            else:  # 429 / quota 403: this key is out of quota for now
                _cool(b, "cool_keys", name, COOL_KEY_S)
                print(f"  gemini[{purpose}]: {slot}/{model} rate-limited ({msg}); next key")
            _save_budget(b)
            time.sleep(1.0)
            continue
        msg = result[1]
        _log(purpose, model, slot, err=msg)
        b = _budget()
        _cool(b, "cool_pairs", f"{name}|{model}", COOL_PAIR_S)
        _save_budget(b)
        print(f"  gemini[{purpose}]: {slot}/{model} failed ({msg}); next key/model")
        time.sleep(1.0)
    print(f"  gemini[{purpose}]: {per_call} requests for one call without an answer; using heuristics")
    return None


# ---------------------------------------------------------------- helpers

def load_trends(repo: Path) -> dict:
    p = repo / "trends" / "latest.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def generate_text(prompt: str, *, purpose: str = "", search: bool = False, timeout: float = 90.0) -> str | None:
    """Free-text call, optionally grounded with Google Search (used by the trends job). Never raises."""
    if not available():
        return None
    b = _budget()
    mx_calls = int(os.environ.get("GEMINI_MAX_CALLS", "5"))
    mx_http = int(os.environ.get("GEMINI_MAX_HTTP", "16"))
    if b.get("calls", 0) >= mx_calls:
        return None
    b["calls"] = b.get("calls", 0) + 1
    _save_budget(b)
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if search:
        payload["tools"] = [{"google_search": {}}]
    delay = 5.0
    plan = [(m, True) for m in _models()[:1]] if search else []
    plan += [(m, False) for m in _ordered_models()]
    deadline = time.time() + float(os.environ.get("GEMINI_DEADLINE_S", "300"))
    for name, key in _ordered_keys():
        slot = _key_slot(name)
        for model, with_search in plan:
            if time.time() > deadline:
                print(f"  gemini[{purpose}]: deadline reached; giving up")
                return None
            b = _budget()
            if b.get("http", 0) >= mx_http:
                print(f"  gemini[{purpose}]: http budget exhausted")
                return None
            b["http"] = b.get("http", 0) + 1
            _save_budget(b)
            p2 = dict(payload)
            if not with_search:
                p2.pop("tools", None)
            body = json.dumps(p2).encode()
            result = _post(model, key, body, timeout)
            kind = result[0]
            if kind == "ok":
                txt = _text_from_response(result[1])
                if txt:
                    b = _budget()
                    b["good_model"] = model
                    b["good_key"] = name
                    _save_budget(b)
                    print(f"  gemini[{purpose}]: ok via {slot}/{model}")
                    return txt
            elif kind == "http":
                code, raw = result[1], result[2]
                if code == 400:
                    print(f"  gemini[{purpose}]: HTTP {code}; giving up")
                    return None
                if code in (401, 403) and not _quota_error(code, raw):
                    print(f"  gemini[{purpose}]: {slot} HTTP {code} (auth); next key")
                    break  # next key
                if code == 404:
                    continue
                print(f"  gemini[{purpose}]: {slot}/{model} HTTP {code}; next model/key")
                if _quota_error(code, raw):
                    time.sleep(2)
                    continue
            else:
                print(f"  gemini[{purpose}]: {slot}/{model} {result[1]}; backoff")
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return None
