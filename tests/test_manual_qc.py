import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manual_qc

GOOD = ["no_logo", "no_burned_text", "audio"]


def _setup(tmp, r, entry):
    (tmp / "results").mkdir(); (tmp / "config").mkdir()
    (tmp / "results" / f"{r['id']}.json").write_text(json.dumps(r))
    (tmp / "config" / "manual_qc.json").write_text(json.dumps({"approved": [entry]}))


def _held(**kw):
    r = {"id": "stock-x-01", "status": "retry_queued", "asset_url": "https://x/y.mp4", "qc_pending": True,
         "qc_status": "skipped", "qa": {"audio": True}, "gate": {"ok": False, "failures": ["gemini QC skipped (unavailable/budget)"]}}
    r.update(kw); return r


def test_approves_held_render(tmp_path):
    _setup(tmp_path, _held(), {"id": "stock-x-01", "approved_by": "bibi", "checks": GOOD})
    assert manual_qc.apply(tmp_path) == ["stock-x-01"]
    r = json.loads((tmp_path / "results" / "stock-x-01.json").read_text())
    assert r["status"] == "done" and not r["qc_pending"] and r["gate"]["ok"] is True
    assert manual_qc.apply(tmp_path) == []  # idempotent


def test_never_overrides_hard_failures_or_bad_entries(tmp_path):
    bad = _held(gate={"ok": False, "failures": ["no audio track"]})
    _setup(tmp_path, bad, {"id": "stock-x-01", "approved_by": "bibi", "checks": GOOD})
    assert manual_qc.apply(tmp_path) == []
    _setup(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, _held(), {"id": "stock-x-01", "approved_by": "someone", "checks": GOOD})
    assert manual_qc.apply(tmp_path / "b") == []
    _setup(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path, _held(qa={"audio": False}), {"id": "stock-x-01", "approved_by": "bibi", "checks": GOOD})
    assert manual_qc.apply(tmp_path / "c") == []
