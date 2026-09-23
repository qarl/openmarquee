"""Tests for media_probe (Bug 2, 2026-09-22): derive video duration via
ffprobe instead of hardcoding it.

These DRIVE real ffprobe against a real generated MP4 (not a stub/mock)
so the test witnesses the actual probe behavior — a mock would only
encode our assumptions. ffmpeg-dependent cases skip when ffmpeg/ffprobe
aren't on PATH (they always are on the device + dev host); the
failure-path cases run everywhere (they return None with or without
ffprobe).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from openmarquee.media_probe import probe_duration_ms, probe_duration_ms_from_bytes

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not _HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")


def _make_mp4(path: Path, seconds: float) -> None:
    """Encode a real HW-decoder-friendly test clip of a known length."""
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@requires_ffmpeg
def test_probe_duration_ms_matches_real_clip(tmp_path: Path) -> None:
    mp4 = tmp_path / "clip.mp4"
    _make_mp4(mp4, 2.0)
    dur = probe_duration_ms(mp4)
    assert dur is not None
    # Container duration can differ from the requested length by a frame
    # or two; a wide tolerance still proves it's derived (not the old
    # hardcoded 10_000 / default 5000).
    assert abs(dur - 2000) <= 300, f"expected ~2000ms, got {dur}"


@requires_ffmpeg
def test_probe_from_bytes_matches_real_clip(tmp_path: Path) -> None:
    mp4 = tmp_path / "clip.mp4"
    _make_mp4(mp4, 3.0)
    dur = probe_duration_ms_from_bytes(mp4.read_bytes())
    assert dur is not None
    assert abs(dur - 3000) <= 300, f"expected ~3000ms, got {dur}"


def test_probe_missing_file_returns_none(tmp_path: Path) -> None:
    assert probe_duration_ms(tmp_path / "does-not-exist.mp4") is None


def test_probe_garbage_bytes_returns_none() -> None:
    # The ftyp-only fake MP4 that test_seed uses has no timed stream →
    # no readable duration → None (caller falls back). Must never raise.
    fake = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 120
    assert probe_duration_ms_from_bytes(fake) is None


def test_probe_empty_bytes_returns_none() -> None:
    assert probe_duration_ms_from_bytes(b"") is None
