"""Media probing helpers (ffprobe).

Bug 2 (2026-09-22): video content duration was hardcoded (seed:
`duration_ms=10_000`; upload: the client payload default) instead of
derived from the media. A 4.75s clip shown for 10s is wrong. Derive the
real duration from the asset with ffprobe at ingest (seed + upload).

Kept intentionally tiny + dependency-light: it shells out to `ffprobe`
(already on the device — it ships with ffmpeg) and returns whole
milliseconds, or None when ffprobe is unavailable / the file has no
readable duration. Callers fall back to a sane default on None rather
than failing the ingest.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ffprobe should return in well under a second for a local file; the
# timeout guards against a pathological/hung invocation blocking ingest.
_FFPROBE_TIMEOUT_S = 15


def probe_duration_ms(path: Path | str) -> int | None:
    """Return the media duration at `path` in whole milliseconds via
    ffprobe, or None if ffprobe is absent, the call fails, or the file
    reports no positive duration.

    Never raises — a probe failure returns None so the caller can fall
    back to a default rather than aborting a seed/upload.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.warning("probe_duration_ms: ffprobe not on PATH; cannot derive duration")
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("probe_duration_ms: ffprobe failed for %s", path)
        return None
    raw = result.stdout.strip()
    try:
        seconds = float(raw)
    except ValueError:
        # "N/A" or empty for a file with no timed streams.
        logger.warning("probe_duration_ms: no numeric duration for %s (got %r)", path, raw)
        return None
    if seconds <= 0:
        return None
    return int(round(seconds * 1000))


def probe_duration_ms_from_bytes(data: bytes) -> int | None:
    """Same as `probe_duration_ms`, for in-memory bytes (the upload
    path). Writes a temp file because ffprobe needs to seek the MP4
    `moov` box, which a stdin pipe can't do reliably. Returns None on
    any failure.
    """
    if not data:
        return None
    return _with_temp_mp4(data, probe_duration_ms, "probe_duration_ms_from_bytes")


def probe_video_dimensions(path: Path | str) -> tuple[int, int] | None:
    """Return the (width, height) in pixels of the first video stream at
    `path` via ffprobe, or None if ffprobe is absent, the call fails, or
    the file reports no readable video dimensions.

    Never raises — a probe failure returns None (Bug 1b server-side guard,
    2026-09-22). The caller decides policy on None; the upload guard
    accepts on None because the device always has ffprobe (it ships with
    ffmpeg) and the browser ffmpeg.wasm transcode is the primary ≤720p
    guarantee — None only happens in a degraded/dev env without ffprobe,
    where failing the upload would be worse than accepting it.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.warning("probe_video_dimensions: ffprobe not on PATH; cannot read dimensions")
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("probe_video_dimensions: ffprobe failed for %s", path)
        return None
    # csv=p=0 on the first video stream yields a single line "W,H".
    raw = result.stdout.strip().splitlines()
    if not raw:
        logger.warning("probe_video_dimensions: no video stream for %s", path)
        return None
    parts = raw[0].split(",")
    if len(parts) < 2:
        logger.warning("probe_video_dimensions: unparseable dims for %s (got %r)", path, raw[0])
        return None
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        logger.warning("probe_video_dimensions: non-integer dims for %s (got %r)", path, raw[0])
        return None
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def probe_video_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    """Same as `probe_video_dimensions`, for in-memory bytes (the upload
    path). Returns None on any failure.
    """
    if not data:
        return None
    return _with_temp_mp4(data, probe_video_dimensions, "probe_video_dimensions_from_bytes")


def _with_temp_mp4(data: bytes, probe, label: str):
    """Write `data` to a temp .mp4, run `probe(path)`, always clean up.

    ffprobe needs to seek the MP4 `moov` box, which a stdin pipe can't do
    reliably, so in-memory probes spill to a temp file. Shared by the
    duration + dimensions byte-probes. Returns None on temp-write failure.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            # Record the path BEFORE writing: delete=False means the file
            # exists on disk the moment it's opened, so a mid-write failure
            # (e.g. ENOSPC) must still hit the `finally` cleanup below.
            tmp_path = tmp.name
            tmp.write(data)
        return probe(tmp_path)
    except OSError:
        logger.exception("%s: temp write failed", label)
        return None
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except OSError:
                logger.warning("%s: temp cleanup failed for %s", label, tmp_path)
