"""Server-side video poster (first-frame PNG) regeneration.

Motivation (2026-07-03, qarl / Jason handover): the poster
(asset.png) for a video slide was ONLY generated CLIENT-SIDE by
`ui/src/video-upload.js` — it captured the transcoded video's first
frame onto a canvas + uploaded that as the thumbnail. Every other
path that touched the mp4 (server-side re-encode, the 720p uploader
clamp, playlist restore pointing at re-rendered content, etc.) left
the OLD poster in place. QA observed on Jason's device: 17 posters
~20 days stale (2026-06-06 posters vs 2026-06-25/26 videos).

This module regenerates asset.png from the mp4's first frame
whenever `ContentStorage.save_video` writes the mp4, so the poster
stays in lock-step with the video regardless of which upstream
path produced the new bytes.

Implementation:

  * `regenerate_video_poster_png(mp4_bytes, *, fallback_png=None)`
    is a pure function: bytes in, PNG bytes out. Test-friendly —
    no filesystem side effects.
  * Shells out to `ffmpeg -frames:v 1 -update 1 -f image2` with the
    mp4 fed on stdin (`-i pipe:0`) + the PNG written to a
    `.png`-suffixed tempfile (NOT `.png.new` — ffmpeg infers format
    from the extension, per QA's 2026-07-03 gotcha).
  * On any ffmpeg failure (missing binary, decode error, timeout,
    broken output), falls back to `fallback_png` if provided; else
    raises `PosterRegenerationError`. Save-path callers pass the
    client-uploaded thumbnail as fallback so a dev host without
    ffmpeg preserves pre-fix behavior.
  * A 30-second subprocess timeout bounds runaway ffmpeg processes
    (a 10s Pi decode of a 30MB clip empirically runs ~2s; 30s
    covers pathological cases without wedging save_video).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_FFMPEG_TIMEOUT_S = 30
# Bounded output-size guard so a broken ffmpeg that dumps an
# unbounded frame stream can't ENOSPC the tmpfs. 8 MB covers a
# 1080p uncompressed PNG with headroom; the .png-suffixed output
# is normally ~200 KB - 2 MB for a 1080p video frame at LZ4-tier
# compression.
_POSTER_SIZE_CAP_BYTES = 8 * 1024 * 1024


class PosterRegenerationError(RuntimeError):
    """Raised when server-side poster regen fails AND no fallback
    poster was provided. Callers (ContentStorage.save_video) that
    have a client-supplied thumbnail catch this + degrade to the
    fallback so save_video never wedges."""


def regenerate_video_poster_png(
    mp4_bytes: bytes,
    *,
    fallback_png: bytes | None = None,
) -> bytes:
    """Extract the first video frame of ``mp4_bytes`` as PNG bytes.

    Returns:
        PNG bytes of the first frame on success, or `fallback_png`
        on any ffmpeg failure when a fallback is provided.

    Raises:
        PosterRegenerationError: ffmpeg failed AND no fallback was
        provided. Caller decides whether to bubble to the operator
        (500 the API call) or degrade some other way.

    Design notes:

      * ffmpeg is fed the mp4 on stdin (`-i pipe:0`) so the mp4 doesn't
        have to touch disk before the poster is generated — saves an
        atomic-write round trip during save_video's transactional
        write block. The output CAN'T stream to stdout because
        `-f image2` in single-frame mode wants a real file so it can
        stat the output extension for the encoder pick; hence the
        `.png`-suffixed tempfile.
      * `-update 1` tells ffmpeg to not append the frame counter to
        the filename (pre-6.0 default).
      * `-frames:v 1` caps at one frame; anything else wastes CPU
        + tmpfs.
      * The tempfile lives under `tempfile.gettempdir()` (`/tmp`,
        `/dev/shm` on the Pi if it's mounted tmpfs). Cleanup runs
        in a finally-block so a subprocess crash still frees the
        file.
    """
    if not shutil.which("ffmpeg"):
        return _fallback_or_raise(
            fallback_png,
            "ffmpeg binary not found on PATH; poster regeneration unavailable",
        )
    # Named tempfile with an explicit .png suffix — ffmpeg infers
    # the encoder from the extension. Writing to .png.new (or any
    # other suffix) would produce a raw or "unknown format" error.
    # `delete=False` because ffmpeg opens the file by path AFTER
    # NamedTemporaryFile closes it (we close inside a `with` for the
    # SIM115 context-manager contract); the finally block below is
    # what actually removes it.
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",  # overwrite output (the empty tempfile we just made)
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "pipe:0",
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    "-f",
                    "image2",
                    str(tmp_path),
                ],
                input=mp4_bytes,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            return _fallback_or_raise(
                fallback_png,
                "ffmpeg binary vanished mid-invocation",
            )
        except subprocess.TimeoutExpired:
            return _fallback_or_raise(
                fallback_png,
                f"ffmpeg poster regen timed out after {_FFMPEG_TIMEOUT_S}s",
            )
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:]
            return _fallback_or_raise(
                fallback_png,
                f"ffmpeg poster regen failed (rc={result.returncode}): {stderr_tail!r}",
            )
        try:
            png = tmp_path.read_bytes()
        except OSError as exc:
            return _fallback_or_raise(
                fallback_png,
                f"ffmpeg poster regen output unreadable: {exc!r}",
            )
        if not png:
            return _fallback_or_raise(
                fallback_png,
                "ffmpeg poster regen produced an empty file",
            )
        if len(png) > _POSTER_SIZE_CAP_BYTES:
            # A well-behaved image2/png encoder shouldn't exceed the
            # cap on a single frame; if we're here something is very
            # wrong. Fall back rather than write a giant asset.png.
            return _fallback_or_raise(
                fallback_png,
                f"ffmpeg poster regen output exceeded cap ({len(png)} > "
                f"{_POSTER_SIZE_CAP_BYTES} bytes)",
            )
        return png
    finally:
        # Best-effort cleanup — a leaked tempfile is nowhere near
        # as bad as tripping the save_video transaction, so we
        # swallow OSError from unlink.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _fallback_or_raise(fallback_png: bytes | None, reason: str) -> bytes:
    if fallback_png is not None:
        log.warning("poster regen falling back to caller-supplied thumbnail: %s", reason)
        return fallback_png
    raise PosterRegenerationError(reason)
