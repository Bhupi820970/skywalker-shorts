"""YouTube direct-upload publish path (no network)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import buffer_post as bp  # noqa: E402
import yt_upload as yt  # noqa: E402

IST = yt.IST
ENV = {"SKY_YT_CLIENT_ID": "cid", "SKY_YT_CLIENT_SECRET": "csec", "SKY_YT_REFRESH_TOKEN": "rt"}


def test_configured_needs_all_three():
    assert yt.yt_configured(ENV)
    assert not yt.yt_configured({**ENV, "SKY_YT_REFRESH_TOKEN": " "})
    assert not yt.yt_configured({})


def test_video_body_public_not_for_kids_with_credits_and_tags():
    r = {"title": "Why <Gojo> smiled 💀 | JJK", "tags": ["#jjk", "anime", "anime", "gojo"],
         "description_final": "Hook.\nSource: official clip.\n\nMusic:\nSong: X - Y"}
    b = yt.video_body(r)
    assert b["status"]["privacyStatus"] == "public"
    assert b["status"]["selfDeclaredMadeForKids"] is False
    assert "publishAt" not in b["status"]
    assert b["snippet"]["title"] == "Why Gojo smiled 💀 | JJK"
    assert "Music:\nSong: X - Y" in b["snippet"]["description"]
    assert b["snippet"]["tags"] == ["jjk", "anime", "gojo"]
    assert b["snippet"]["categoryId"] == "24"


def test_video_body_scheduled_is_private_with_publish_at():
    slot = datetime(2026, 11, 22, 6, 30, tzinfo=IST)
    b = yt.video_body({"title": "t", "tags": "['a', 'b']"}, publish_at=slot)
    assert b["status"]["privacyStatus"] == "private"
    assert b["status"]["publishAt"] == "2026-11-22T01:00:00.000Z"
    assert b["snippet"]["tags"] == ["a", "b"]


def test_tags_respect_youtube_500_char_limit():
    tags = [f"tag{i:03d}" + "x" * 40 for i in range(30)]
    out = yt.yt_tags(tags)
    assert sum(len(t) for t in out) + len(out) - 1 <= 480 and len(out) < 30


def test_schedule_for():
    now = datetime(2026, 11, 21, 12, 0, tzinfo=IST)
    assert yt.schedule_for(now + timedelta(hours=2), now) == now + timedelta(hours=2)
    assert yt.schedule_for(now + timedelta(minutes=5), now) is None
    assert yt.schedule_for(now - timedelta(minutes=30), now) is None


@pytest.mark.parametrize("code,body", [
    (403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}),
    (403, {"error": {"errors": [{"reason": "uploadLimitExceeded"}], "message": "limit"}}),
    (429, {"error": {"message": "slow down"}}),
])
def test_quota_errors_classified(code, body):
    with pytest.raises(yt.QuotaError):
        yt.classify_http_error(code, json.dumps(body))


def test_auth_and_other_errors_classified():
    with pytest.raises(yt.AuthError):
        yt.classify_http_error(400, json.dumps({"error": "invalid_grant"}))
    with pytest.raises(RuntimeError) as e:
        yt.classify_http_error(400, json.dumps({"error": {"errors": [{"reason": "invalidTitle"}], "message": "bad"}}))
    assert not isinstance(e.value, (yt.QuotaError, yt.AuthError))


def test_resumable_upload_resumes_after_308(monkeypatch, tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 1000)
    monkeypatch.setattr(yt, "CHUNK", 400)
    calls = []

    def fake_open(req, timeout=120):
        calls.append((req.get_method(), dict(req.header_items())))
        if req.get_method() == "POST":
            body = json.loads(req.data)
            assert body["status"]["selfDeclaredMadeForKids"] is False
            return 200, {"Location": "https://upload.example/session"}, b""
        rng = req.get_header("Content-range")
        end = int(rng.split("-")[1].split("/")[0])
        if end < 999:
            return 308, {"Range": f"bytes=0-{end}"}, b""
        return 200, {}, json.dumps({"id": "VID123", "status": {"privacyStatus": "private"}}).encode()

    monkeypatch.setattr(yt, "_open", fake_open)
    v = yt.resumable_upload("tok", str(f), yt.video_body({"title": "t"}))
    assert v["id"] == "VID123"
    ranges = [h.get("Content-range") for m, h in calls if m == "PUT"]
    assert ranges == ["bytes 0-399/1000", "bytes 400-799/1000", "bytes 800-999/1000"]


def test_resumable_upload_quota_on_init(monkeypatch, tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x" * 10)
    monkeypatch.setattr(yt, "_open", lambda req, timeout=120: (
        403, {}, json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}}).encode()))
    with pytest.raises(yt.QuotaError):
        yt.resumable_upload("tok", str(f), {})


# ---------------------------------------------------------------- main() flow

def _result(iid, slot, title, **kw):
    r = {"id": iid, "status": "done", "slot": slot.isoformat(), "title": title, "hook": title,
         "asset_url": f"https://github.com/x/y/releases/download/render-{iid}/render-{iid}.mp4",
         "description_final": f"{title}\nSource: official clip.\n\nMusic:\nSong: A - B", "tags": ["anime"],
         "source": f"src-{iid}"}
    r.update(kw)
    return r


@pytest.fixture
def env(monkeypatch, tmp_path):
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(yt, "REPO", tmp_path)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    for k in ("DRY_RUN", "IDS", "FORCE", "BUFFER_API_KEY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(yt, "access_token", lambda: "tok")
    monkeypatch.setattr(yt, "my_channel", lambda tok: {"snippet": {"title": "Skywalker"}})
    monkeypatch.setattr(yt, "channel_uploads", lambda tok, ch: [])
    monkeypatch.setattr(yt, "api_get", lambda tok, path, params: {"items": []})
    monkeypatch.setattr(yt, "download", lambda url, dest: dest)
    monkeypatch.setattr(bp, "metadata_errors", lambda r: [])
    state = {"uploads": [], "plan": []}

    def fake_upload(tok, path, body, notify=True):
        state["uploads"].append(body)
        nxt = state["plan"].pop(0) if state["plan"] else None
        if isinstance(nxt, Exception):
            raise nxt
        return {"id": f"V{len(state['uploads'])}", "status": {"privacyStatus": body["status"]["privacyStatus"]}}

    monkeypatch.setattr(yt, "resumable_upload", fake_upload)

    def write(r):
        (tmp_path / "results" / f"{r['id']}.json").write_text(json.dumps(r))

    def read(iid):
        return json.loads((tmp_path / "results" / f"{iid}.json").read_text())

    state["write"], state["read"] = write, read
    return state


def _now():
    return datetime.now(IST).replace(second=0, microsecond=0)


def test_upload_schedules_future_slot_and_records_ledger(env):
    slot = _now() + timedelta(hours=3)
    env["write"](_result("a", slot, "Why Gojo smiled"))
    assert yt.main([]) == 0
    r = env["read"]("a")
    body = env["uploads"][0]
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"]
    assert r["yt_video_id"] == "V1" and r["buffer_post_id"] == "yt:V1"
    assert r["buffer_status"] == "scheduled" and r["yt_status"] == "scheduled"
    assert r["youtube_url"] == "https://youtube.com/shorts/V1"
    assert yt.main([]) == 0 and len(env["uploads"]) == 1  # idempotent


def test_due_slot_uploads_public_now(env):
    env["write"](_result("b", _now() - timedelta(minutes=20), "Why Denji ran"))
    assert yt.main([]) == 0
    assert env["uploads"][0]["status"]["privacyStatus"] == "public"
    r = env["read"]("b")
    assert r["buffer_status"] == "sent" and r["yt_status"] == "public"


def test_quota_error_requeues_and_stops_run(env):
    env["write"](_result("c1", _now() + timedelta(hours=2), "Why Asta yelled"))
    env["write"](_result("c2", _now() + timedelta(hours=4), "Why Yuno left"))
    env["plan"].append(yt.QuotaError("quotaExceeded"))
    assert yt.main([]) == 0
    assert len(env["uploads"]) == 1  # stopped after quota
    for iid in ("c1", "c2"):
        r = env["read"](iid)
        assert r["yt_status"] == "quota_wait" and r["limit_deferred"] is True
        assert not r.get("publish_dead") and r.get("buffer_status") != "dead"
        assert "yt_video_id" not in r
    assert yt.main([]) == 0  # next run retries both
    assert env["read"]("c1")["yt_video_id"] and env["read"]("c2")["yt_video_id"]


def test_other_failure_retries_once_then_dead(env):
    env["write"](_result("d", _now() + timedelta(hours=2), "Why Eren froze"))
    env["plan"] += [RuntimeError("HTTP 400 invalidTitle"), RuntimeError("HTTP 400 invalidTitle")]
    assert yt.main([]) == 1
    r = env["read"]("d")
    assert r["yt_fail_count"] == 1 and not r.get("publish_dead")
    assert yt.main([]) == 1
    r = env["read"]("d")
    assert r["publish_dead"] is True and r["status"] == "done"
    assert yt.main([]) == 0 and len(env["uploads"]) == 2  # dead ones are skipped


def test_buffer_queued_result_left_alone(env):
    env["write"](_result("e", _now() + timedelta(hours=2), "Why Tanjiro cried",
                         buffer_post_id="buf123", buffer_status="scheduled"))
    assert yt.main([]) == 0 and env["uploads"] == []


def test_failed_buffer_post_goes_direct(env):
    env["write"](_result("f", _now() + timedelta(hours=2), "Why Levi stayed",
                         buffer_post_id="buf9", buffer_status="error", buffer_error="daily posting limit"))
    assert yt.main([]) == 0
    r = env["read"]("f")
    assert r["buffer_prev_post_id"] == "buf9" and r["buffer_post_id"] == "yt:V1"
    assert "buffer_error" not in r


def test_repeat_guard_blocks_same_hook(env):
    env["write"](_result("g1", _now() - timedelta(days=1), "Why Hodaka cannot stop watching her",
                         buffer_post_id="yt:OLD", buffer_status="sent", youtube_url="https://youtube.com/shorts/OLD"))
    env["write"](_result("g2", _now() + timedelta(hours=2), "Why Hodaka cannot stop watching her"))
    assert yt.main([]) == 0
    assert env["uploads"] == []
    assert env["read"]("g2")["publish_blocked"].startswith("repeat")


def test_title_already_on_channel_not_reuploaded(env, monkeypatch):
    monkeypatch.setattr(yt, "channel_uploads", lambda tok, ch: [
        {"id": "yt:X", "status": "sent", "metadata": {"title": "Why Makima smiled at Denji"}}])
    env["write"](_result("h", _now() + timedelta(hours=2), "Why Makima smiled at Denji"))
    assert yt.main([]) == 0 and env["uploads"] == []


def test_missed_slot_not_uploaded(env):
    env["write"](_result("i", _now() - timedelta(hours=8), "Why Luffy laughed"))
    assert yt.main([]) == 1 and env["uploads"] == []


def test_force_skips_late_guard_only_for_listed_ids(env, monkeypatch):
    env["write"](_result("late1", _now() - timedelta(hours=8), "Why Luffy laughed"))
    env["write"](_result("late2", _now() - timedelta(hours=8), "Why Zoro stayed"))
    monkeypatch.setenv("FORCE", "1")
    assert yt.main([]) == 1 and env["uploads"] == []  # force without ids still blocked
    monkeypatch.setenv("IDS", "late1")
    assert yt.main([]) == 0
    assert len(env["uploads"]) == 1 and env["read"]("late1")["yt_video_id"]
    assert "yt_video_id" not in env["read"]("late2")


def test_demo_and_unassigned_stock_never_uploaded(env):
    env["write"](_result("demo-x", _now() + timedelta(hours=2), "Why Naruto ran"))
    env["write"](_result("stock-y", _now() + timedelta(hours=2), "Why Sasuke left"))
    assert yt.main([]) == 0 and env["uploads"] == []


def test_refresh_marks_scheduled_as_sent_when_public(env, monkeypatch):
    slot = _now() - timedelta(minutes=10)
    env["write"](_result("j", slot, "Why Rengoku smiled", publish_via="youtube", yt_video_id="VJ",
                         yt_status="scheduled", yt_publish_at=slot.isoformat(), buffer_post_id="yt:VJ",
                         buffer_status="scheduled", buffer_due_at="2026-11-21T01:00:00.000Z"))
    monkeypatch.setattr(yt, "api_get", lambda tok, path, params: {
        "items": [{"id": "VJ", "status": {"privacyStatus": "public", "uploadStatus": "processed"}}]})
    assert yt.main([]) == 0
    r = env["read"]("j")
    assert r["buffer_status"] == "sent" and r["yt_status"] == "public"


def test_refresh_flags_video_kept_private(env, monkeypatch, capsys):
    slot = _now() - timedelta(hours=2)
    env["write"](_result("k", slot, "Why Zoro got lost", publish_via="youtube", yt_video_id="VK",
                         yt_status="scheduled", yt_publish_at=slot.isoformat(), buffer_post_id="yt:VK",
                         buffer_status="scheduled"))
    monkeypatch.setattr(yt, "api_get", lambda tok, path, params: {
        "items": [{"id": "VK", "status": {"privacyStatus": "private", "uploadStatus": "processed"}}]})
    assert yt.main([]) == 0
    assert env["read"]("k")["yt_restricted"] is True
    assert "::warning::k: still private" in capsys.readouterr().out


def test_auth_failure_exits_nonzero_without_touching_results(env, monkeypatch):
    def boom():
        raise yt.AuthError("token refresh failed (HTTP 400 invalid_grant)")
    monkeypatch.setattr(yt, "access_token", boom)
    env["write"](_result("l", _now() + timedelta(hours=2), "Why Goku trained"))
    assert yt.main([]) == 1 and env["uploads"] == []


def test_auth_check_prints_only_title_and_handle(monkeypatch, capsys):
    monkeypatch.setattr(yt, "access_token", lambda: "secret-access-token")
    monkeypatch.setattr(yt, "my_channel", lambda tok: {"id": "UC1", "snippet": {
        "title": "Skywalker", "customUrl": "@skywalker-k3i", "description": "d"}})
    assert yt.main(["--auth-check"]) == 0
    out = capsys.readouterr().out
    assert out == "channel: Skywalker\nhandle: @skywalker-k3i\n"


def test_publish_workflow_switches_on_secrets():
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "scripts/yt_upload.py" in wf and "scripts/buffer_post.py" in wf
    for k in yt.SECRET_ENVS:
        assert f"secrets.{k}" in wf
    assert "if: secrets" not in wf and "pull_request" not in wf
    auth = (ROOT / ".github" / "workflows" / "yt-auth-check.yml").read_text()
    assert "workflow_dispatch" in auth and "--auth-check" in auth and "pull_request" not in auth
    for w in (wf, auth):
        for line in w.splitlines():
            if "uses:" in line:
                ref = line.split("@", 1)[1].split()[0]
                assert len(ref) == 40, line
