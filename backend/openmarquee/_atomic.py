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
from pathlib import Path

# Owner-only read+write. World-readable would expose the AP
# password / station password / Tailscale auth key in
# openmarquee-settings.json on disk.
DEFAULT_MODE = 0o600


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
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = DEFAULT_MODE,
) -> None:
    """Same shape as atomic_write_text, for binary payloads
    (PNG / MP4 asset bytes). Default 0600 still applies -- the
    on-device venv user is the only consumer."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
