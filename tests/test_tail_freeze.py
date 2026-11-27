"""Tail freeze QC: a held last frame of >=0.8s must fail auto_qc.check_tail_freeze."""
from __future__ import annotations

import subprocess
from pathlib import Path

import auto_qc


def _make(tmp: Path, name: str, vf: str, t: float = 3.0) -> Path:
    p = tmp / name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=30:d={t}",
         "-f", "lavfi", "-i", "sine=f=440:d=3", "-vf", vf, "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p)],
        check=True, capture_output=True,
    )
    return p


def test_held_tail_is_detected(tmp_path):
    # 1.5s of moving noise, then 1.5s of a cloned last frame
    p = _make(tmp_path, "held.mp4",
              "geq=lum='if(lt(T,1.5),random(1)*255,128)':cb=128:cr=128,tpad=stop_mode=clone:stop_duration=1.5",
              t=1.5)
    # tpad may not apply after geq duration; build explicitly
    moving = tmp_path / "m.mp4"
    still = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","testsrc=s=1080x1920:r=30:d=2",
                    "-f","lavfi","-i","sine=f=440:d=2","-pix_fmt","yuv420p","-c:v","libx264",
                    "-preset","ultrafast","-c:a","aac","-shortest",str(moving)],check=True,capture_output=True)
    subprocess.run(["ffmpeg","-y","-ss","1.9","-i",str(moving),"-loop","1","-t","1.5",
                    "-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-an",str(still)],
                   check=True,capture_output=True)
    # silent still has no audio; concat video then add audio
    held = tmp_path / "held2.mp4"
    lst = tmp_path / "c.txt"
    # simpler: ffmpeg filter tpad on moving
    subprocess.run(["ffmpeg","-y","-i",str(moving),"-vf","tpad=stop_mode=clone:stop_duration=1.5",
                    "-af","apad=pad_dur=1.5","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p",
                    "-c:a","aac","-shortest",str(held)],check=True,capture_output=True)
    got = auto_qc.check_tail_freeze(held, window_s=3.0, min_dur=0.8)
    assert got and got["duration"] >= 0.8, got
    report = auto_qc.check(held)
    assert any("tail freeze" in x for x in report["issues"])


def test_moving_tail_passes(tmp_path):
    moving = tmp_path / "ok.mp4"
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","testsrc=s=1080x1920:r=30:d=4",
                    "-f","lavfi","-i","sine=f=440:d=4","-pix_fmt","yuv420p","-c:v","libx264",
                    "-preset","ultrafast","-c:a","aac","-shortest",str(moving)],check=True,capture_output=True)
    assert auto_qc.check_tail_freeze(moving, window_s=3.0, min_dur=0.8) is None


def test_end_card_default_is_off():
    import pipeline
    assert float(pipeline.END_CARD) == 0.0
