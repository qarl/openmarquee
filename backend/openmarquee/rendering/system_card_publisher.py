"""PR3 (2026-06-27) — SystemCardPublisher: the adapter that lets the
network supervisor push RenderSystemCard / ClearSystemCard ops onto
the live Renderer.

PR3 fix-pass F2 (2026-07-01) — SINGLE serialized worker + LATEST-WINS.

Prior design (per-call daemon thread) was fire-and-forget and
unordered: a dropped or reordered `clear()` (e.g. a
`DEGRADED → ONLINE` transition where the clear lost the RLock race
to a still-in-flight `render(DEGRADED)`) could leave a persistent
wrong card ("Lost the wifi connection" while the sign was actually
online) for up to the 60-min max-lifetime cap.

New design:
    * The publisher holds a single "desired state" slot: either a
      pending params dict, `_CLEAR` for the clear intent, or
      `_NO_PENDING` when nothing new is queued.
    * `render(params)` and `clear()` overwrite the desired state
      (latest-wins) and wake a single dedicated worker thread.
    * The worker loops: read + drain the desired state, forward to
      the renderer, retry on failure so the LATEST intent always
      eventually applies. Failures are logged with a grep-able
      `[system-card-publisher]` prefix (F2 minor / observability).
    * The 60-min max-lifetime cap in the renderer overlay stays
      as a backstop against a persistently-wedged renderer.

The publisher's methods are non-blocking; the supervisor's sync
`apply_event` returns immediately + the async observe loop stays
responsive.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


# Sentinel: no request is currently pending on the desired-state
# slot. Distinct from `None` (which represents an explicit clear
# intent) and from a params-dict (render intent).
_NO_PENDING = object()

# Sentinel: the caller asked for `clear_system_card()`. Storing a
# named object rather than `None` so the recorder tests can round-
# trip cleanly if they ever inspect the slot directly.
_CLEAR = object()

# Retry backoff on renderer errors. Kept short enough that a
# transient IPC blip doesn't sit visible for long, but long enough
# that a fully wedged renderer doesn't spin CPU on retries.
_RETRY_BACKOFF_S = 2.0

# Timeout for the worker's join() on shutdown. The current design
# never blocks longer than one renderer IPC round-trip inside the
# worker (5-15s worst case on cold-start), but shutdown is best-
# effort — we don't want a daemon thread wedge to hold up the
# process exit.
_WORKER_JOIN_TIMEOUT_S = 5.0


class SystemCardPublisher:
    """Adapter mapping the supervisor's publisher contract onto the
    Renderer Protocol methods. Single-worker + latest-wins per F2.
    """

    def __init__(self, renderer: Any) -> None:
        self._renderer = renderer
        self._lock = threading.Lock()
        self._pending: object = _NO_PENDING
        # `_last_applied` is the state we most recently pushed to
        # the renderer AND received an OK response for. Compared
        # against `_pending` inside the worker so a redundant
        # request (same params as the last-applied) is coalesced
        # into a no-op instead of hammering the renderer with
        # duplicate IPC.
        self._last_applied: object = _NO_PENDING
        self._event = threading.Event()
        self._stopping = False
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="system-card-publisher",
            daemon=True,
        )
        self._thread.start()

    def render(self, params: dict) -> None:
        """Enqueue a render intent. Latest-wins: overwrites any
        previously-queued render or clear. Non-blocking — the
        caller returns immediately + the worker forwards the
        latest state to the renderer.
        """
        # Snapshot the dict so a mutating caller can't race the
        # worker's read.
        snapshot = dict(params)
        with self._lock:
            self._pending = snapshot
        self._event.set()

    def clear(self) -> None:
        """Enqueue a clear intent. Latest-wins."""
        with self._lock:
            self._pending = _CLEAR
        self._event.set()

    def shutdown(self, *, timeout: float = _WORKER_JOIN_TIMEOUT_S) -> None:
        """Best-effort worker termination. Only called from tests
        (production keeps the publisher alive for the process
        lifetime)."""
        self._stopping = True
        self._event.set()
        self._thread.join(timeout=timeout)

    def _worker_loop(self) -> None:
        """Single-threaded drain of the desired-state slot. Runs
        forever as a daemon; exits when `_stopping` is set."""
        while True:
            # Wait for a new request or a shutdown signal.
            self._event.wait()
            self._event.clear()
            if self._stopping:
                return
            # Drain the pending slot inside the lock so a burst of
            # render() / clear() calls between the wait and the
            # apply collapses to the LATEST one (that's F2's whole
            # point: latest-wins).
            with self._lock:
                desired = self._pending
                self._pending = _NO_PENDING
            if desired is _NO_PENDING:
                # Spurious wake (nothing new). Loop back to wait.
                continue
            if desired is self._last_applied:
                # Same intent as what we most recently applied
                # successfully. Skip the IPC round-trip.
                continue
            self._apply(desired)

    def _apply(self, desired: object) -> None:
        """Push one desired-state to the renderer. On failure,
        re-queue the desired state so the LATEST intent eventually
        applies (unless a NEWER render()/clear() has landed in the
        meantime — in which case that newer intent wins)."""
        try:
            if desired is _CLEAR:
                self._renderer.clear_system_card()
            else:
                # `desired` is a params dict.
                assert isinstance(desired, dict)
                self._renderer.render_system_card(desired)
        except Exception:  # noqa: BLE001 — grep-able log + retry
            log.warning(
                "[system-card-publisher] apply failed for %s; "
                "requeueing for retry after %.1fs backoff",
                "clear" if desired is _CLEAR else "render",
                _RETRY_BACKOFF_S,
                exc_info=True,
            )
            # Re-queue this intent ONLY if no newer intent has
            # landed. Newer intent wins → the failed attempt is
            # dropped, which is the correct latest-wins semantics.
            with self._lock:
                if self._pending is _NO_PENDING:
                    self._pending = desired
            # Backoff before the next attempt so a fully wedged
            # renderer doesn't spin CPU. The event is already set
            # (either by the requeue above OR by the caller who
            # just posted a newer intent), so the wait() returns
            # immediately once we finish sleeping.
            time.sleep(_RETRY_BACKOFF_S)
            self._event.set()
            return
        # Success — record and log at debug level.
        self._last_applied = desired
        log.debug(
            "[system-card-publisher] applied %s",
            "clear" if desired is _CLEAR else "render",
        )
