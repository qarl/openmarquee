"""Recovery A1: consume the power-cycle Setup-Mode boot-hint.

The boot oneshot ``system/openmarquee-boot-gesture.sh`` writes
``/run/openmarquee/boot-hint`` = ``"setup"`` when the operator power-cycles
the sign 3× in rapid succession (each boot cut short before the ~20s
stable-boot clear window). This module lets the backend read that hint at
supervisor startup and force the sign into Setup Mode so a phone can
reconnect it — no console, no cables.

The hint lives in tmpfs so it never survives a reboot (a hint is
per-boot). Cleanup is owned by the root ``openmarquee-boot-gesture-clear``
timer at +20s; the backend's own unlink is best-effort because under
``ProtectSystem=strict`` the backend user cannot remove a file inside the
0750 ``root:openmarquee`` ``/run/openmarquee`` directory. Re-reading a
not-yet-cleared hint is harmless: forcing Setup Mode when the supervisor
is already in SETUP is a no-op (the state machine has no SETUP-self edge).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_BOOT_HINT_PATH = "/run/openmarquee/boot-hint"


def _hint_path() -> Path:
    """The boot-hint path; env-overridable for tests / dev hosts that
    have no /run/openmarquee."""
    return Path(os.environ.get("OPENMARQUEE_BOOT_HINT_PATH", DEFAULT_BOOT_HINT_PATH))


def read_boot_hint() -> str | None:
    """Return the boot-hint string (e.g. ``"setup"``), or None when the
    hint is absent or unreadable. Best-effort: any I/O error (no file, no
    directory on a dev host, permission) resolves to None so a missing
    hint never breaks supervisor startup."""
    path = _hint_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return None
    return text or None


def consume_boot_hint() -> str | None:
    """Read the boot-hint and best-effort remove it. Returns the hint
    string or None.

    Deletion is best-effort by design: on the Pi the backend user cannot
    unlink inside /run/openmarquee, so the root clear-timer is the
    guaranteed cleanup and re-consuming an uncleared hint is idempotent
    (forcing SETUP while already in SETUP is a no-op). In tests / dev the
    unlink succeeds and consumes immediately.
    """
    hint = read_boot_hint()
    if hint is not None:
        try:
            _hint_path().unlink()
        except OSError:
            # Expected on-device (dir not group-writable); the clear-timer
            # removes it at +20s. Logged at debug so it isn't noise.
            log.debug("boot-hint unlink not permitted; clear-timer will remove it")
    return hint
