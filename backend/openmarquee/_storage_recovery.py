"""Shared corruption-recovery helper for the JSON storage classes
(Batch 19.2 / sweep #10 #4).

Each file-backed storage class (PlaylistStorage, ScheduleStorage,
FlockStorage, SettingsStorage, TombstoneStorage) reads JSON +
Pydantic-validates on load. When the on-disk file is corrupt
(half-written, edited by hand, truncated by a power-loss-during-
write window the atomic-write pattern doesn't cover), the load
raises and the entire backend startup crashes.

The recovery contract:

  1. Detect: catch JSONDecodeError + Pydantic ValidationError at
     the parse boundary.
  2. Quarantine: rename the bad file to
     `<original>.corrupt-<UTC-ISO-timestamp>` so it's preserved for
     postmortem but won't re-trip the load.
  3. Log: WARN level so operator + on-call see it.
  4. Fall back: caller returns its defaults type; the next save()
     overwrites the original path with a valid file.

This file owns step (2)+(3). Steps (1)+(4) live in each storage
class's load() so they can construct the right default.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def quarantine_corrupt_file(path: Path, exc: Exception) -> Path | None:
    """Rename `path` to a timestamped `.corrupt-<UTC-ISO>` sibling.

    Returns the quarantine target path on success, None on failure
    (e.g. read-only filesystem, missing parent dir). Failure logs but
    doesn't raise -- the caller's "return defaults" fallback still
    completes even when quarantine itself can't preserve the bad
    file. Better to bring the service up against fresh defaults than
    leave it hard-crashing on every restart.

    Filename shape: <name>.corrupt-2026-05-11T14:30:00Z so multiple
    quarantine events on the same path don't collide (UTC second
    granularity).
    """
    if not path.exists():
        return None
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    corrupt = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        path.rename(corrupt)
    except OSError:
        log.exception(
            "Storage at %s failed to parse AND quarantine-rename to %s "
            "failed; the next save() will overwrite the bad file in "
            "place. Original parse error: %s",
            path,
            corrupt,
            exc,
        )
        return None
    log.warning(
        "Storage at %s failed to parse; quarantined to %s; starting "
        "fresh from defaults. Original parse error: %s",
        path,
        corrupt,
        exc,
    )
    return corrupt
