"""PR3 fix-pass F2 (2026-07-01) — SystemCardPublisher single-worker
+ latest-wins tests.

The prior per-call daemon-thread design was fire-and-forget and
unordered; a dropped or reordered clear could leave a persistent
wrong card. The rewrite (single worker + latest-wins) is what this
suite pins.
"""

from __future__ import annotations

import threading
import time

import pytest

from openmarquee.rendering.system_card_publisher import (
    _CLEAR,
    SystemCardPublisher,
)


class _RecordingRenderer:
    """Records every render_system_card / clear_system_card call.
    Serialization is a threading.Lock — the worker is a single
    thread, so the recorder just captures the strict order the
    worker applied intents in.

    Optionally raises for the first N calls to simulate a transient
    IPC blip; the worker's retry path should recover and eventually
    apply the LATEST intent.
    """

    def __init__(self, *, fail_first_n: int = 0):
        self.lock = threading.Lock()
        self.calls: list[str] = []  # "render:<kind>" or "clear"
        self._remaining_fails = fail_first_n

    def render_system_card(self, params: dict) -> None:
        with self.lock:
            if self._remaining_fails > 0:
                self._remaining_fails -= 1
                raise RuntimeError("simulated transient IPC failure")
            self.calls.append(f"render:{params.get('kind', '?')}")

    def clear_system_card(self) -> None:
        with self.lock:
            if self._remaining_fails > 0:
                self._remaining_fails -= 1
                raise RuntimeError("simulated transient IPC failure")
            self.calls.append("clear")


def _wait_for_calls(
    recorder: _RecordingRenderer,
    at_least: int,
    *,
    timeout: float = 5.0,
) -> None:
    """Poll the recorder until it has at_least this many calls, or
    until timeout. Keeps tests deterministic without sleeping-hoping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with recorder.lock:
            n = len(recorder.calls)
        if n >= at_least:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"expected at_least={at_least} calls, got {len(recorder.calls)} within {timeout:.1f}s"
    )


def _stop(pub: SystemCardPublisher) -> None:
    pub.shutdown(timeout=2.0)


def test_render_then_clear_ends_cleared():
    """Ordered render→clear ends with the clear applied and remains
    cleared. This is the trivial case; F2's harder case is
    out-of-order under contention, tested below."""
    r = _RecordingRenderer()
    pub = SystemCardPublisher(r)
    try:
        pub.render({"kind": "SETUP"})
        pub.clear()
        _wait_for_calls(r, at_least=1)
        # Give the worker a moment to drain any coalesced intent so
        # we assert on the settled state.
        time.sleep(0.1)
        assert r.calls, "worker never applied anything"
        assert r.calls[-1] == "clear"
    finally:
        _stop(pub)


def test_latest_wins_coalesces_burst_intents():
    """A burst of render() + clear() while the worker is busy MUST
    collapse to the LATEST intent — the worker doesn't fire every
    intermediate call, just the one the caller most recently asked
    for."""
    r = _RecordingRenderer()
    pub = SystemCardPublisher(r)
    try:
        # Fire a burst before the worker can drain any of them.
        # Depending on scheduling the first one MIGHT already be
        # in flight, but the LAST one (clear) MUST end up applied.
        pub.render({"kind": "SETUP"})
        pub.render({"kind": "CONNECTING"})
        pub.render({"kind": "DEGRADED"})
        pub.clear()
        _wait_for_calls(r, at_least=1)
        time.sleep(0.15)
        assert r.calls[-1] == "clear", f"expected clear as the settled state; got calls={r.calls}"
    finally:
        _stop(pub)


def test_out_of_order_render_then_clear_race_ends_cleared():
    """F2 canonical case: a supervisor transition to ONLINE fires
    clear() while the previous render(DEGRADED) is still in flight.
    In the OLD design the clear could "lose" and leave the sign
    stuck showing DEGRADED. The single-worker + latest-wins design
    guarantees the last-posted intent ALWAYS wins."""
    r = _RecordingRenderer()
    pub = SystemCardPublisher(r)
    try:
        for _ in range(10):
            pub.render({"kind": "DEGRADED", "variant": "lost"})
            pub.clear()
        _wait_for_calls(r, at_least=1)
        time.sleep(0.2)
        assert r.calls[-1] == "clear"
    finally:
        _stop(pub)


def test_retries_on_transient_failure_and_eventually_applies():
    """When the renderer raises transiently, the worker requeues +
    retries so the LATEST intent still eventually applies. The
    supervisor's state machine never wedges on the renderer."""
    r = _RecordingRenderer(fail_first_n=2)
    pub = SystemCardPublisher(r)
    try:
        pub.render({"kind": "SETUP"})
        # Two failures + a success. Backoff is 2s between retries;
        # allow a generous timeout so this passes even on slow CI.
        _wait_for_calls(r, at_least=1, timeout=8.0)
        time.sleep(0.05)
        assert r.calls == ["render:SETUP"]
    finally:
        _stop(pub)


def test_worker_stays_singleton_no_thread_pileup():
    """Under a burst that would previously spawn N daemon threads,
    the new design uses exactly one worker for the publisher's
    lifetime."""
    r = _RecordingRenderer()
    before = threading.active_count()
    pub = SystemCardPublisher(r)
    try:
        for _ in range(100):
            pub.render({"kind": "SETUP"})
            pub.clear()
        _wait_for_calls(r, at_least=1, timeout=5.0)
        time.sleep(0.15)
        after = threading.active_count()
        # We expect ONE extra thread (the worker) — allow a small
        # +2 slack for whatever pytest / logging spawns concurrently
        # in the shared runtime.
        assert after - before <= 3, f"thread pileup detected: before={before}, after={after}"
    finally:
        _stop(pub)


def test_clear_sentinel_is_distinct_from_render_none():
    """The _CLEAR sentinel is a distinct object identity so a
    caller cannot accidentally trigger clear via `render(None)`.
    (render(None) would raise TypeError before reaching the worker
    because dict(None) is invalid, but this pins the invariant.)"""
    assert _CLEAR is not None
    with pytest.raises(TypeError):
        r = _RecordingRenderer()
        pub = SystemCardPublisher(r)
        try:
            pub.render(None)  # type: ignore[arg-type]
        finally:
            _stop(pub)
