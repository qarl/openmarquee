"""Stability arc Layer 2 (2026-06-23): minimal sd_notify helper.

Dependency-free implementation per QA decision (2): avoid adding
`cysystemd` or any other PyPI dep on the production Pi. Just open a
SOCK_DGRAM connection to `$NOTIFY_SOCKET` and `sendto(b"WATCHDOG=1")`
when systemd's `WatchdogSec=` is active.

Behavior when systemd is not the parent (dev/CI/etc.):
  - `$NOTIFY_SOCKET` is unset → all notify_* calls are no-ops.
  - Cheap: an unset-env check + early return; no socket created.

Behavior when systemd is the parent (production):
  - `$NOTIFY_SOCKET` is set, e.g. `/run/systemd/notify` (abstract
    namespace not currently supported in this impl since the
    /run socket is what FYS's systemd uses; if a future system
    uses abstract sockets, extend to handle '@'-prefixed paths).
  - Connection-less SOCK_DGRAM: no handshake, sendto is best-effort.
    If the kernel drops the datagram (unlikely on local AF_UNIX),
    the next iteration's ping covers it — WatchdogSec is sized
    with margin.

Reference: systemd(1) sd_notify(3) man page.

Used by: `playback._loop` (per-painted-frame ping); other long-
running asyncio tasks can call from their main heartbeat.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

log = logging.getLogger(__name__)


# Throttle the per-ping cadence. The playback loop calls advance()
# ~30 times/second; we don't need to ping systemd at that rate.
# Pinging once per second is plenty for `WatchdogSec=60s` (60x
# margin). Throttle reduces syscall volume and lets the
# heartbeat batch with whatever the kernel is happy to coalesce.
_THROTTLE_INTERVAL_S = 1.0


class _NotifyState:
    """Lazy-initialized socket + throttle state.

    One instance per process (module-level singleton). Thread-safe
    by lock; calls from asyncio tasks are fine (asyncio runs single-
    threaded by default but the throttle state read/write is cheap
    enough to lock unconditionally).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._path: str | None = None
        self._last_send_t: float = 0.0
        # Disabled until init() runs. Persists across init() calls
        # in case the env changes between checks.
        self._enabled: bool | None = None

    def _ensure_init(self) -> bool:
        """First-call init: read $NOTIFY_SOCKET, open the socket.

        Returns True if notifications can be sent; False if disabled
        (env unset, abstract socket unsupported, etc.).
        """
        if self._enabled is not None:
            return self._enabled
        path = os.environ.get("NOTIFY_SOCKET")
        if not path:
            self._enabled = False
            log.debug("sd_notify: $NOTIFY_SOCKET unset; disabled")
            return False
        # Abstract namespace ('@' prefix) replaces the leading '@'
        # with a NUL byte. Not currently used on FYS but support is
        # trivial — extend `bytes(...)` below if abstract sockets
        # are needed.
        if path.startswith("@"):
            log.warning(
                "sd_notify: abstract socket path %r not yet supported; "
                "disabling watchdog notifications",
                path,
            )
            self._enabled = False
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError as e:
            log.warning("sd_notify: failed to create AF_UNIX socket: %s", e)
            self._enabled = False
            return False
        self._sock = sock
        self._path = path
        self._enabled = True
        log.info("sd_notify: enabled path=%s", path)
        return True

    def notify_watchdog(self) -> None:
        """Send WATCHDOG=1 to systemd, throttled to once per second.

        Best-effort: failures are logged at DEBUG and swallowed.
        sendto on a SOCK_DGRAM is non-blocking + cheap on AF_UNIX.
        """
        with self._lock:
            if not self._ensure_init():
                return
            now = time.monotonic()
            if now - self._last_send_t < _THROTTLE_INTERVAL_S:
                return
            try:
                assert self._sock is not None and self._path is not None
                self._sock.sendto(b"WATCHDOG=1", self._path)
                self._last_send_t = now
            except OSError as e:
                log.debug("sd_notify: sendto failed: %s", e)

    def notify_ready(self) -> None:
        """Send READY=1 to systemd (Type=notify handshake).

        Called once at backend startup once the playback loop is
        live + healthy. Without this, Type=notify hangs on startup
        waiting for the handshake.
        """
        with self._lock:
            if not self._ensure_init():
                return
            try:
                assert self._sock is not None and self._path is not None
                self._sock.sendto(b"READY=1", self._path)
                log.info("sd_notify: READY=1 sent")
            except OSError as e:
                log.warning("sd_notify: READY sendto failed: %s", e)


_STATE = _NotifyState()


def notify_watchdog() -> None:
    """Throttled per-painted-frame WATCHDOG=1 ping. See _NotifyState."""
    _STATE.notify_watchdog()


def notify_ready() -> None:
    """One-shot READY=1 handshake. Call once on backend startup."""
    _STATE.notify_ready()
