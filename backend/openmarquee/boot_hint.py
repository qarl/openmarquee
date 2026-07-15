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


# --- Recovery A1 countdown: the boot-card "Restart N× more" line ------------
#
# The same oneshot that writes the tmpfs setup hint also maintains an on-DISK
# rapid-power-cycle counter at /var/openmarquee/boot-cycle-count. It's on disk,
# NOT tmpfs, precisely because a hard power pull wipes tmpfs — the count has to
# survive the very power cut it's counting. The oneshot runs BEFORE the backend
# and zeroes the counter the instant it fires the hint, so by the time we read
# it here the count is only ever 0..threshold-1 — exactly the "N cycles done,
# threshold-N to go" state the boot card renders as a countdown.

DEFAULT_BOOT_CYCLE_COUNT_PATH = "/var/openmarquee/boot-cycle-count"
DEFAULT_BOOT_GESTURE_THRESHOLD = 3


def _count_path() -> Path:
    """The boot-cycle-count path. Env-overridable via the SAME variable the
    shell oneshot reads (``OPENMARQUEE_BOOT_CYCLE_COUNT_FILE``) so a test can
    point both at one temp file, and a dev host without /var/openmarquee just
    reads 0."""
    return Path(os.environ.get("OPENMARQUEE_BOOT_CYCLE_COUNT_FILE", DEFAULT_BOOT_CYCLE_COUNT_PATH))


def _threshold() -> int:
    """Rapid-cycle count that arms Setup Mode. Env-overridable via the shell
    oneshot's ``OPENMARQUEE_BOOT_GESTURE_THRESHOLD`` so the countdown math
    always matches the gesture. Falls back to the default on anything
    unparseable or nonsensical (< 2 would make a countdown meaningless).

    The sub-2 floor is a display-only guard and is deliberately NOT mirrored
    in the shell oneshot: with a shell threshold of 1 the oneshot resets the
    counter on every increment, so the backend only ever reads count 0 →
    boot_countdown_hint returns None regardless. The divergence can never
    surface a wrong countdown."""
    try:
        t = int(os.environ.get("OPENMARQUEE_BOOT_GESTURE_THRESHOLD", ""))
    except (TypeError, ValueError):
        return DEFAULT_BOOT_GESTURE_THRESHOLD
    return t if t >= 2 else DEFAULT_BOOT_GESTURE_THRESHOLD


def read_boot_cycle_count() -> int:
    """Return the rapid-power-cycle count written by
    ``openmarquee-boot-gesture.sh``, or 0 when the file is
    absent/unreadable/corrupt.

    Mirrors the shell oneshot's own guard (``read_count``): a blank or
    non-numeric file — e.g. a write torn by the very power cut being counted —
    resolves to 0 so a bad counter can never break startup or render a garbage
    countdown. Fail-soft, like ``read_boot_hint``."""
    try:
        text = _count_path().read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return 0
    try:
        n = int(text)
    except ValueError:
        return 0
    return n if n >= 0 else 0


def boot_countdown_hint() -> str | None:
    """The boot-card countdown line for a partially-completed power-cycle
    gesture — ``"Restart N× more for Setup Mode"`` — or None when nothing
    should show.

    Because the oneshot runs before us and zeroes the counter the moment it
    fires, the on-disk count here is 0..threshold-1::

        count 0          -> None   (normal boot, or just-fired / just-cleared)
        count 1 (thr 3)  -> "Restart 2× more for Setup Mode"
        count 2 (thr 3)  -> "Restart 1× more for Setup Mode"

    Visibility caveat (inherent, harmless): the boot card only renders after
    the ~18s cold-EGL warm-up, and the clear timer zeroes the counter at +20s —
    so the countdown shows only on a "patient" boot where the operator waits for
    the card. On a genuinely rapid power-cycle it never renders, but the gesture
    still works (the count accumulates regardless of the card). See
    ``system/openmarquee-boot-gesture.sh``."""
    threshold = _threshold()
    count = read_boot_cycle_count()
    if 1 <= count < threshold:
        return f"Restart {threshold - count}× more for Setup Mode"
    return None
