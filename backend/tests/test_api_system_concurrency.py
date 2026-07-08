"""Batch 6.2 concurrency tests: a slow subprocess call (fbset, iw,
airport, tailscale, iwgetid) must NOT block the asyncio event loop
or a concurrent fast request will sit waiting for it.

Before Batch 6.2 these handlers called subprocess.run() directly --
that's a blocking syscall under asyncio, so a `/api/system/wifi-scan`
that took 8s would also stall a parallel `/api/playback/state`.
After 6.2 the slow paths are wrapped in `asyncio.to_thread(...)`,
which runs them on the default executor and keeps the loop free.

The tests use httpx.ASGITransport (FastAPI's recommended in-process
async client) so we exercise the real asyncio scheduling, not the
sync TestClient's behavior.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from openmarquee.app import app


@pytest.mark.asyncio
async def test_slow_wifi_scan_doesnt_block_concurrent_state_polls(
    monkeypatch: pytest.MonkeyPatch,
):
    """Drive a slow `iw scan` while a fast `/api/playback/state`
    fires concurrently. The state response should return in well
    under the subprocess's 8s timeout (and ideally <100ms on a
    machine that's not under load).

    Implementation: monkeypatch subprocess.run to sleep, then patch
    shutil.which so the iw branch is taken. The state endpoint
    doesn't go through subprocess so its timing is purely loop-
    scheduling latency.
    """
    import openmarquee.api_system as api_sys

    slow_seconds = 1.0  # short enough for a quick test; tests the principle

    class FakeCompleted:
        # subprocess.CompletedProcess-shaped: full field set so any
        # subprocess-consuming path reached during this monkey-patch's
        # scope is satisfied. Historically only `stdout` + `returncode`
        # were set (the wifi-scan handler is the only path this test
        # nominally exercises), but pytest-observed thread pollution
        # from an autostart `apply_enabled` background thread caused
        # `wifi_station._run_nmcli` to hit `result.stderr or ""` and
        # blow up with `AttributeError: 'FakeCompleted' object has no
        # attribute 'stderr'` mid-CI-run (surfaced as an unhandled-
        # thread-exception warning, 2026-07-02). Adding the empty
        # `stderr` here + the `args` slot (subprocess writes it on the
        # return object) makes the mock forward-safe for any other
        # subprocess consumer a monkey-patched sibling test spawns
        # a thread into.
        # 2026-07-07: nmcli is now the PRIMARY scan path (returns the full
        # list where `iw` on an NM-managed radio returned only the
        # associated BSS). Give the mock a valid `nmcli -t` line so the
        # scan returns after the ONE (primary) nmcli call — keeping this a
        # single-slow-subprocess exercise rather than nmcli-empty-then-iw
        # (two slow calls would oversaturate the executor and delay the
        # concurrent state poll this test guards).
        stdout = "TestNet:70:2412 MHz\n"
        stderr = ""
        returncode = 0
        args: list[str] = []

    def slow_run(*_args, **_kwargs):
        time.sleep(slow_seconds)
        return FakeCompleted()

    monkeypatch.setattr(api_sys.subprocess, "run", slow_run)
    monkeypatch.setattr(api_sys.shutil, "which", lambda name: "/usr/sbin/iw")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        # Fire the slow one first, then the fast one. If the slow
        # call were still sync, gather() would wait for it to
        # finish before the loop could schedule the fast call --
        # total wall time ≈ slow_seconds + state_time.
        # With asyncio.to_thread the loop interleaves and the fast
        # call returns ~immediately after launch.
        scan_task = asyncio.create_task(client.get("/api/system/wifi-scan"))
        # Give the slow scan a moment to get scheduled + enter to_thread.
        await asyncio.sleep(0.05)
        state_resp = await client.get("/api/playback/state")
        # THE invariant — deterministic + wall-clock-free: the fast state
        # poll returned while the slow (`slow_seconds`) scan was STILL in
        # flight. If the event loop had been blocked by a synchronous
        # subprocess.run, the state poll could not have completed until the
        # scan finished, so `scan_task` would already be done here. Capture
        # it the instant the state poll returns, before we await the scan.
        #
        # (The prior wall-clock assertion `state_elapsed < slow_seconds/2`
        # flaked on a loaded CI box: the loop wasn't actually blocked, but
        # scheduling latency pushed the non-blocked poll past the fixed
        # threshold. Relative ordering is load-immune — the scan sleeps a
        # full second, orders of magnitude above any scheduling jitter.)
        scan_still_running = not scan_task.done()
        await scan_task  # let it finish so the client + test clean up

    assert state_resp.status_code == 200
    assert scan_still_running, (
        "state poll didn't return until the slow wifi-scan finished -- the "
        "event loop was blocked (wifi-scan subprocess not offloaded to a thread)"
    )
