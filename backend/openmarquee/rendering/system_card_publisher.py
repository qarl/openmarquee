"""PR3 (2026-06-27) — SystemCardPublisher: the adapter that lets the
network supervisor push RenderSystemCard / ClearSystemCard ops onto
the live Renderer.

The Renderer Protocol grew `render_system_card(params)` and
`clear_system_card()` methods in this PR; the supervisor calls those
methods through this thin adapter (with `.render(params)` and
`.clear()`) so the supervisor's actuator/publisher slot stays a small
duck-typed object contract — matching how the AP lifecycle actuator
is shaped.

Failure containment: exceptions bubble up to the supervisor's
`_fire_system_card_for_transition`, which catches them and emits a
warn diagnostic. The state machine never wedges on a renderer outage.
"""

from __future__ import annotations

from typing import Any


class SystemCardPublisher:
    """Adapter mapping the supervisor's publisher contract onto the
    Renderer Protocol methods. Kept as a plain object (not a Protocol)
    because there is only one concrete impl; the mock supervisor
    tests inject their own recording stub.
    """

    def __init__(self, renderer: Any) -> None:
        self._renderer = renderer

    def render(self, params: dict) -> None:
        """Forward to `renderer.render_system_card(params)`.

        The Rust IPC layer clamps params before layout, so a chatty
        supervisor cannot grow paint-side costs — the Python side
        can pass params through verbatim.
        """
        self._renderer.render_system_card(params)

    def clear(self) -> None:
        """Forward to `renderer.clear_system_card()`."""
        self._renderer.clear_system_card()
