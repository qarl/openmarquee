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
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            # Record the path BEFORE writing: delete=False means the file
            # exists on disk the moment it's opened, so a mid-write failure
            # (e.g. ENOSPC) must still hit the `finally` cleanup below.
            tmp_path = tmp.name
            tmp.write(data)
        return probe_duration_ms(tmp_path)
    except OSError:
        logger.exception("probe_duration_ms_from_bytes: temp write failed")
        return None
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except OSError:
                logger.warning("probe_duration_ms_from_bytes: temp cleanup failed for %s", tmp_path)
