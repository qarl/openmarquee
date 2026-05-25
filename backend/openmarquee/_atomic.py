"""Shared atomic-write helper (Batch 11.2 / sweep #5 #6).

Every file-backed storage class in this codebase serializes with
the same pattern: write to a sibling `.tmp` file, then atomically
rename over the target. Sweep #5 added two requirements on top:

  1. Set mode 0600 on the rename target (settings.json especially:
     contains the AP password, the future Tailscale auth key, the
     wifi station password). The captive-portal AP runs as a non-
     privileged service user; without explicit chmod, world-
     readability depends on the umask of whatever shell created
     the venv.
  2. Clean up the orphan `.tmp` if `replace()` fails (Batch 9.2
     pattern; carried forward here).

This helper centralizes both so future storage classes inherit the
discipline automatically.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path

# Owner-only read+write. World-readable would expose the AP
# password / station password / Tailscale auth key in
# openmarquee-settings.json on disk.
DEFAULT_MODE = 0o600


def _atomic_write(
    path: Path,
    writer: Callable[[Path], None],
    *,
    mode: int,
) -> None:
    """Shared write-then-rename ladder for atomic_write_text +
    atomic_write_bytes. `writer(tmp)` does the payload-shaped write
    (text vs bytes); this function owns tmp naming, chmod, rename,
    and cleanup-on-failure. Keeping all three behaviors in one place
    means any future storage class that wraps these helpers inherits
    the same discipline -- and a future hardening pass only has one
    site to touch.

    Round-29: per-call tmp filename suffix (".tmp.<pid>.<uuid12>").
    Pre-r29 the suffix was the fixed string ".tmp" -- two concurrent
    callers writing to the same path would share the SAME staging
    file. Even with serialized callers at the storage-class layer
    (ContentStorage threading.Lock added in r29 part A), a future
    caller that forgets the lock would corrupt the staging bytes.
    The per-call unique suffix makes the staging files trample-free
    by construction.

    Orphan .tmp.<pid>.<hex> files from a crashed write can be swept
    at boot via _storage_recovery (TODO: add a per-storage sweep on
    lifespan startup; out of scope for this commit). Unlike the
    fixed-".tmp" pre-r29 shape, orphans here don't block the next
    write -- they're cosmetic clutter only.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:12]}")
    try:
        writer(tmp)
        os.chmod(tmp, mode)
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int = DEFAULT_MODE,
) -> None:
    """Atomically replace `path` with `text`. Cleans up the .tmp
    on rename failure; sets the target file mode (default 0600)
    before the replace so the brief window when the file exists
    under its final name is never wider-than-intended."""
    _atomic_write(path, lambda tmp: tmp.write_text(text, encoding="utf-8"), mode=mode)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = DEFAULT_MODE,
) -> None:
    """Same shape as atomic_write_text, for binary payloads
    (PNG / MP4 asset bytes). Default 0600 still applies -- the
    on-device venv user is the only consumer."""
    _atomic_write(path, lambda tmp: tmp.write_bytes(data), mode=mode)
