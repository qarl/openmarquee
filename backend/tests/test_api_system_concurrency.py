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
import threading

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

    # 2026-07-08 (QA): the scan mock blocks on a TEST-CONTROLLED Event
    # instead of a wall-clock sleep. That makes the scan PROVABLY still
    # running when the concurrent state poll returns — the test releases it
    # only AFTER the assertion — so there is no timing race. The earlier
    # wall-clock (`state_elapsed < 0.5s`) and relative-ordering
    # (`not scan_task.done()` vs a 1s sleep) versions both still required the
    # non-blocking poll to WIN a race against a fixed scan duration, which
    # CPU starvation under heavy load could still lose (~1/25) even though
    # the loop was never actually blocked. Event-gating removes the race.
    scan_running = threading.Event()  # scan sets this once it's executing
    release_scan = threading.Event()  # test sets this to let the scan finish

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

    # subprocess.run is patched process-wide, so calls OTHER than the
    # wifi-scan reach this mock too — notably the per-request `tailscale
    # status` in the FQDN-redirect middleware, which sits in the state
    # poll's own request path. Block ONLY on the scan command; return an
    # immediate benign result for everything else so the state poll isn't
    # itself stalled behind the Event.
    scan_progs = {"iw", "nmcli", "iwlist", "airport"}

    def blocking_run(cmd=None, *_args, **_kwargs):
        argv = cmd if isinstance(cmd, (list, tuple)) else []
        prog = str(argv[0]) if argv else ""
        if prog in scan_progs:
            # THE scan under test: executes on the executor thread when the
            # handler offloads via asyncio.to_thread. Block until the test
            # releases it; the timeout is a safety net so a test error can't
            # hang the worker thread forever.
            scan_running.set()
            release_scan.wait(timeout=10.0)
        return FakeCompleted()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        # Patch AFTER the app's lifespan startup so a startup/autostart
        # subprocess call (e.g. the network-supervisor boot probe or the
        # wifi_station apply thread) doesn't itself block on our Event and
        # stall the whole test on the release safety-net — only the
        # wifi-scan handler under test should hit blocking_run.
        monkeypatch.setattr(api_sys.subprocess, "run", blocking_run)
        monkeypatch.setattr(api_sys.shutil, "which", lambda name: "/usr/sbin/iw")
        scan_task = asyncio.create_task(client.get("/api/system/wifi-scan"))
        # Wait (off the loop) until the scan is actually executing inside the
        # mock — i.e. offloaded to the executor and now blocked on
        # release_scan. If the handler did NOT offload (the regression), the
        # loop is stuck inside blocking_run and this only resumes once the
        # 10s release safety-net fires; the scan then finishes and the
        # assertion below catches the already-done scan_task.
        assert await asyncio.to_thread(scan_running.wait, 10.0), "scan never started"
        # The scan is now PROVABLY still running — release_scan hasn't been
        # set, so blocking_run cannot have returned. A free event loop
        # returns this state poll immediately; the scan therefore CANNOT
        # have finished, so scan_task.done() is deterministically False.
        # No dependence on the poll "winning" against any wall-clock.
        state_resp = await client.get("/api/playback/state")
        scan_still_running = not scan_task.done()
        # Release the scan + let it finish so the client + test clean up.
        release_scan.set()
        await scan_task

    assert state_resp.status_code == 200
    assert scan_still_running, (
        "state poll didn't return while the wifi-scan was still blocked -- "
        "the event loop was blocked (wifi-scan subprocess not offloaded to a thread)"
    )
