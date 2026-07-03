"""Server-side video-poster regeneration.

Motivation: qarl 2026-07-03 observed 17 stale posters on Jason's
device — asset.png was ONLY generated client-side at video-upload
time, so any other path that touched the mp4 (server re-encode,
720p clamp, playlist restore) left the poster stale. The fix
regenerates asset.png from the mp4's first frame via ffmpeg on
every write of asset.mp4. This test suite pins the helper's
happy path (via monkey-patched subprocess.run so ffmpeg doesn't
need to be present on the test host) AND every fallback branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openmarquee.content.poster import (
    PosterRegenerationError,
    regenerate_video_poster_png,
)


class _FakeCompleted:
    """subprocess.CompletedProcess-shaped: returncode + stderr are read
    by regenerate_video_poster_png; stdout is unused."""

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""
        self.args: list[str] = []


def _install_fake_ffmpeg(monkeypatch, write_bytes: bytes | None, returncode: int = 0):
    """Monkey-patch subprocess.run so it "runs ffmpeg" without
    actually running ffmpeg. If write_bytes is not None, the output
    tempfile path (positional last arg) gets those bytes written to
    it before subprocess.run "returns" — mimicking ffmpeg's normal
    behavior of writing to the file it was told to write."""

    def _fake_run(cmd, **kwargs):
        # Emulate ffmpeg's write behavior: last cmd arg is the
        # output tempfile path.
        if write_bytes is not None:
            Path(cmd[-1]).write_bytes(write_bytes)
        return _FakeCompleted(returncode=returncode, stderr=b"simulated ffmpeg stderr")

    monkeypatch.setattr("openmarquee.content.poster.subprocess.run", _fake_run)
    # `shutil.which("ffmpeg")` gates the whole helper — make it
    # return a truthy path so the fake ffmpeg is reached.
    monkeypatch.setattr(
        "openmarquee.content.poster.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )


class TestRegenerateVideoPosterPng:
    def test_returns_ffmpeg_output_bytes_on_success(self, monkeypatch):
        """The happy path: ffmpeg wrote a PNG to the tempfile, we
        read it back + return the bytes. Simulated bytes are the
        1-byte PNG magic prefix — not a real image, but the helper
        doesn't care."""
        _install_fake_ffmpeg(monkeypatch, write_bytes=b"\x89PNG_from_ffmpeg")
        out = regenerate_video_poster_png(b"fake mp4 bytes")
        assert out == b"\x89PNG_from_ffmpeg"

    def test_falls_back_when_ffmpeg_binary_missing(self, monkeypatch):
        """Dev hosts without ffmpeg: helper returns the fallback
        instead of raising. Pre-fix behavior (client thumbnail
        used verbatim) is preserved on such hosts."""
        monkeypatch.setattr("openmarquee.content.poster.shutil.which", lambda _name: None)
        out = regenerate_video_poster_png(b"anything", fallback_png=b"\x89PNG_fallback")
        assert out == b"\x89PNG_fallback"

    def test_raises_when_ffmpeg_binary_missing_and_no_fallback(self, monkeypatch):
        monkeypatch.setattr("openmarquee.content.poster.shutil.which", lambda _name: None)
        with pytest.raises(PosterRegenerationError) as exc:
            regenerate_video_poster_png(b"anything")
        assert "ffmpeg binary not found" in str(exc.value)

    def test_falls_back_on_ffmpeg_non_zero_exit(self, monkeypatch):
        _install_fake_ffmpeg(monkeypatch, write_bytes=None, returncode=1)
        out = regenerate_video_poster_png(b"corrupt mp4", fallback_png=b"\x89PNG_fallback")
        assert out == b"\x89PNG_fallback"

    def test_falls_back_when_ffmpeg_writes_empty_file(self, monkeypatch):
        """ffmpeg exited 0 but produced 0 bytes (weird corner). Don't
        write an empty asset.png — degrade to the fallback."""
        _install_fake_ffmpeg(monkeypatch, write_bytes=b"", returncode=0)
        out = regenerate_video_poster_png(b"mp4", fallback_png=b"\x89PNG_fallback")
        assert out == b"\x89PNG_fallback"

    def test_falls_back_when_ffmpeg_output_exceeds_cap(self, monkeypatch):
        """A misbehaving encoder that dumps >8 MB for a single frame:
        we fall back rather than blow away tmpfs headroom or the
        Pi's memory."""
        oversized = b"A" * (8 * 1024 * 1024 + 1)
        _install_fake_ffmpeg(monkeypatch, write_bytes=oversized)
        out = regenerate_video_poster_png(b"mp4", fallback_png=b"\x89PNG_fallback")
        assert out == b"\x89PNG_fallback"

    def test_falls_back_on_ffmpeg_timeout(self, monkeypatch):
        """A wedged ffmpeg (hangs on a broken container) must not
        stall save_video indefinitely. `subprocess.TimeoutExpired`
        surfaces as a fallback swap."""

        def _timeout(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=30)

        monkeypatch.setattr("openmarquee.content.poster.subprocess.run", _timeout)
        monkeypatch.setattr(
            "openmarquee.content.poster.shutil.which",
            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
        )
        out = regenerate_video_poster_png(b"mp4", fallback_png=b"\x89PNG_fallback")
        assert out == b"\x89PNG_fallback"

    def test_output_written_to_png_suffixed_tempfile(self, monkeypatch):
        """qarl 2026-07-03 gotcha: ffmpeg infers encoder from the
        output extension. If the tempfile were e.g. `asset.png.new`
        ffmpeg would emit an unknown-format error. Assert the
        command line ends in a `.png`-suffixed path."""
        captured = {}

        def _fake_run(cmd, **_kwargs):
            captured["cmd"] = list(cmd)
            Path(cmd[-1]).write_bytes(b"\x89PNG_captured")
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.content.poster.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.content.poster.shutil.which",
            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
        )
        regenerate_video_poster_png(b"mp4")
        assert captured["cmd"][-1].endswith(".png"), (
            f"tempfile must have .png extension so ffmpeg picks the PNG "
            f"encoder; got {captured['cmd'][-1]!r}"
        )

    def test_tempfile_is_cleaned_up_after_success(self, monkeypatch):
        """Successful regen must not leak the tempfile. The helper
        removes it in a finally-block; verify the path no longer
        exists once regen returns."""
        captured_path: dict[str, str] = {}

        def _fake_run(cmd, **_kwargs):
            captured_path["path"] = cmd[-1]
            Path(cmd[-1]).write_bytes(b"\x89PNG_ok")
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr("openmarquee.content.poster.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.content.poster.shutil.which",
            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
        )
        regenerate_video_poster_png(b"mp4")
        assert not Path(captured_path["path"]).exists(), (
            "successful regen must clean up its tempfile"
        )

    def test_tempfile_is_cleaned_up_after_failure(self, monkeypatch):
        """Same cleanup contract on the failure path — a failed
        ffmpeg must not leak the tempfile."""
        captured_path: dict[str, str] = {}

        def _fake_run(cmd, **_kwargs):
            captured_path["path"] = cmd[-1]
            return _FakeCompleted(returncode=1)

        monkeypatch.setattr("openmarquee.content.poster.subprocess.run", _fake_run)
        monkeypatch.setattr(
            "openmarquee.content.poster.shutil.which",
            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
        )
        regenerate_video_poster_png(b"mp4", fallback_png=b"fb")
        assert not Path(captured_path["path"]).exists(), "failed regen must clean up its tempfile"
