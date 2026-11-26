#!/usr/bin/env python3
"""Skywalker YouTube OAuth helper (box only, no secrets in this file).

Reads the Desktop OAuth client JSON from $SKY_OAUTH_CLIENT (default ~/.config/skywalker/sky_oauth_client.json,
mode 600; download it from Google Cloud Console > Credentials > OAuth client "Desktop app"; never commit it),
builds a consent URL (youtube.upload + youtube.readonly, offline, prompt=consent,
redirect http://localhost, PKCE), exchanges the pasted code for a refresh token, checks the
token belongs to the expected channel, and stores SKY_YT_CLIENT_ID / SKY_YT_CLIENT_SECRET /
SKY_YT_REFRESH_TOKEN as GitHub secrets with `gh secret set` (values go over stdin; nothing
is printed or written to disk except the PKCE verifier in a mode-600 state file).

Usage:
  python3 sky_oauth.py url
      -> open the printed URL in the browser signed in to the Skywalker channel's account.
         After "Allow" the browser lands on http://localhost/?code=... (page fails to load,
         that's fine). Copy that full address-bar URL (or just the code).
  python3 sky_oauth.py exchange '<full localhost URL or code>' [--expect-handle @skywalker-k3i]
"""
import base64, hashlib, json, os, secrets, stat, subprocess, sys, urllib.error, urllib.parse, urllib.request

_CFG = os.path.join(os.path.expanduser("~"), ".config", "skywalker")
CLIENT = os.environ.get("SKY_OAUTH_CLIENT") or os.path.join(_CFG, "sky_oauth_client.json")
STATE = os.environ.get("SKY_OAUTH_STATE") or os.path.join(_CFG, "sky_oauth_state.json")
REPO = os.environ.get("SKY_GH_REPO", "Bhupi820970/skywalker-shorts")
GH_CONFIG_DIR = os.environ.get("SKY_GH_CONFIG_DIR") or os.environ.get("GH_CONFIG_DIR")  # None = gh default login
REDIRECT = "http://localhost"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
DEFAULT_HANDLE = "@skywalker-k3i"


def _client():
    st = os.stat(CLIENT)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(CLIENT, 0o600)
    d = json.load(open(CLIENT))
    c = d.get("installed") or d.get("web") or d
    if not c.get("client_id") or not c.get("client_secret"):
        sys.exit("client JSON has no client_id/client_secret")
    if "installed" not in d:
        print("warning: client JSON is not a Desktop ('installed') client")
    return c["client_id"], c["client_secret"]


def _write_private(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.chmod(path, 0o600)


def cmd_url():
    cid, _ = _client()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _write_private(STATE, {"verifier": verifier, "state": state})
    q = {"client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    print(AUTH + "?" + urllib.parse.urlencode(q))


def _parse_code(arg):
    arg = arg.strip()
    if arg.startswith("http"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(arg).query)
        if "error" in qs:
            sys.exit("consent returned error: " + qs["error"][0])
        return qs.get("code", [""])[0], qs.get("state", [None])[0]
    return urllib.parse.unquote(arg), None


def _post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error")
        except Exception:
            err = "?"
        sys.exit(f"token endpoint HTTP {e.code}: {err}")


def _channel(access):
    req = urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                 headers={"Authorization": "Bearer " + access})
    with urllib.request.urlopen(req, timeout=60) as r:
        items = json.loads(r.read()).get("items") or []
    if not items:
        sys.exit("this Google account has no YouTube channel")
    sn = items[0]["snippet"]
    return sn.get("title"), sn.get("customUrl") or ""


def _gh_secret(name, value):
    env = dict(os.environ)
    if GH_CONFIG_DIR:
        env["GH_CONFIG_DIR"] = GH_CONFIG_DIR
    p = subprocess.run(["gh", "secret", "set", name, "-R", REPO], input=value.encode(),
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    if p.returncode != 0:
        sys.exit(f"gh secret set {name} failed: {p.stderr.decode(errors='replace')[:200]}")
    print(f"set GitHub secret {name}")


def cmd_exchange(arg, expect):
    cid, csec = _client()
    code, got_state = _parse_code(arg)
    if not code:
        sys.exit("no code found")
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    if got_state and st.get("state") and got_state != st["state"]:
        sys.exit("state mismatch - run `url` again and use the newest link")
    data = {"code": code, "client_id": cid, "client_secret": csec, "redirect_uri": REDIRECT,
            "grant_type": "authorization_code"}
    if st.get("verifier"):
        data["code_verifier"] = st["verifier"]
    tok = _post(TOKEN, data)
    rt = tok.get("refresh_token")
    if not rt:
        sys.exit("no refresh_token returned (consent must use prompt=consent; run `url` again)")
    granted = set((tok.get("scope") or "").split())
    if "https://www.googleapis.com/auth/youtube.upload" not in granted:
        sys.exit("youtube.upload scope was not granted - tick all permissions on the consent screen")
    title, handle = _channel(tok["access_token"])
    print(f"channel: {title}  handle: {handle}")
    if expect and handle.lower() != expect.lower():
        sys.exit(f"expected {expect}; NOT setting secrets (wrong Google account/channel)")
    _gh_secret("SKY_YT_CLIENT_ID", cid)
    _gh_secret("SKY_YT_CLIENT_SECRET", csec)
    _gh_secret("SKY_YT_REFRESH_TOKEN", rt)
    try:
        os.remove(STATE)
    except OSError:
        pass
    print("done - run the yt-auth-check workflow to verify from GitHub")


def main(argv):
    if not argv or argv[0] not in ("url", "exchange"):
        print(__doc__)
        return 2
    if argv[0] == "url":
        cmd_url()
        return 0
    if len(argv) < 2:
        sys.exit("usage: sky_oauth.py exchange '<localhost URL or code>' [--expect-handle @handle]")
    expect = DEFAULT_HANDLE
    if "--expect-handle" in argv:
        expect = argv[argv.index("--expect-handle") + 1]
    cmd_exchange(argv[1], expect)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
