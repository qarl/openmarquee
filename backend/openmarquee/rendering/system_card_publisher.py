"""PR3 (2026-06-27) — SystemCardPublisher: the adapter that lets the
network supervisor push RenderSystemCard / ClearSystemCard ops onto
the live Renderer.

The Renderer Protocol grew `render_system_card(params)` and
`clear_system_card()` methods in this PR; the supervisor calls those
methods through this thin adapter (with `.render(params)` and
`.clear()`) so the supervisor's actuator/publisher slot stays a small
duck-typed object contract — matching how the AP lifecycle actuator
is shaped.

PR3 fix-pass B2 (2026-07-01): the underlying `renderer.render_system_card`
call blocks on the RustRenderer's RLock + JSON readline for up to
~10s (~18s on renderer cold-start). The supervisor is driven from
inside the asyncio observe loop, so a synchronous inline call was
freezing the WHOLE event loop — including the captive-portal HTTP
the user is mid-onboarding on. Fix: the adapter runs the actual IPC
call on a background thread. From the caller's POV the render/clear
returns immediately.

Failure containment: because the IPC call is off-loop, exceptions
inside the thread are LOGGED but not raised back to the supervisor.
Per the fix-pass brief S1 note, the supervisor stays best-effort;
only the preview endpoint (which awaits its own `asyncio.to_thread`)
surfaces failures back to QA.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)


class SystemCardPublisher:
    """Adapter mapping the supervisor's publisher contract onto the
    Renderer Protocol methods. Kept as a plain object (not a Protocol)
    because there is only one concrete impl; the mock supervisor
    tests inject their own recording stub.
    """

    def __init__(self, renderer: Any) -> None:
        self._renderer = renderer

    def render(self, params: dict) -> None:
        """Forward to `renderer.render_system_card(params)` on a
        background thread so the async observe-loop caller stays
        unblocked. Errors inside the thread are logged; caller
        never sees them.

        The Rust IPC layer clamps params before layout, so a chatty
        supervisor cannot grow paint-side costs — the Python side
        can pass params through verbatim.
        """
        # Snapshot params so a caller that mutates the dict after
        # the call doesn't race the background thread.
        snapshot = dict(params)

        def _worker() -> None:
            try:
                self._renderer.render_system_card(snapshot)
            except Exception:  # noqa: BLE001 — best-effort
                log.exception("SystemCardPublisher.render failed on background thread")

        threading.Thread(
            target=_worker,
            name="system-card-render",
            daemon=True,
        ).start()

    def clear(self) -> None:
        """Forward to `renderer.clear_system_card()` on a background
        thread (same rationale as render)."""

        def _worker() -> None:
            try:
                self._renderer.clear_system_card()
            except Exception:  # noqa: BLE001 — best-effort
                log.exception("SystemCardPublisher.clear failed on background thread")

        threading.Thread(
            target=_worker,
            name="system-card-clear",
            daemon=True,
        ).start()
