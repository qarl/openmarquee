"""Pytest config shared by every test in the backend suite."""

import os

import pytest

# Disable the first-boot content seeding for tests. The seed path runs
# inside the FastAPI lifespan, which fires when a TestClient context is
# entered — without this opt-out it would try to populate whatever
# content root the real env var points at (or the default cwd-relative
# fallback) with starter gradient slides, polluting local dev state.
# Seed behavior itself is covered explicitly by `test_seed.py`.
os.environ.setdefault("OPENMARQUEE_DISABLE_SEED", "1")

# Disable the lifespan auto-start of the playback loop. Production runs
# the loop continuously ("hardware always running"); test fixtures
# injecting their own loops via dependency_overrides don't need the
# real singleton leaking an asyncio task across the whole suite.
# Individual tests that need playback running call start() explicitly.
os.environ.setdefault("OPENMARQUEE_DISABLE_AUTOSTART", "1")

# Same reason as DISABLE_AUTOSTART above: the flock pull worker is a
# background asyncio task that reconciles against remote peers on a
# timer. For test fixtures that spin a TestClient it would race
# dependency_override wiring and (worse) try to reach real peers
# stamped in the production flock.json. Tests that need it call
# PullWorker.start() directly on an isolated FlockSync.
os.environ.setdefault("OPENMARQUEE_DISABLE_PULL_WORKER", "1")

# Pin the renderer to the in-process mock so tests don't hit DRM init.
# Without this, every TestClient lifespan tries to open /dev/dri/card0
# (FileNotFoundError on Mac/CI), logs an exception traceback, falls
# back to mock -- functional but noisy. The production wiring's
# fallback path is exercised by an explicit test, not every fixture
# setup.
os.environ.setdefault("OPENMARQUEE_RENDERER", "mock")

# Batch 20.1 / phase A.1: bypass the bearer-token gate so the pre-
# existing 100+ TestClient suites don't need to mint tokens. Auth
# behavior itself is covered explicitly by test_auth.py (which
# clears this env var locally where needed). Production systemd
# unit never sets DISABLE_AUTH; the captive-portal threat model
# assumes the gate is always on.
os.environ.setdefault("OPENMARQUEE_DISABLE_AUTH", "1")


@pytest.fixture(autouse=True)
def _expire_set_password_grace(monkeypatch):
    """Default the Bundle B2 item 7 set-password grace to ALREADY
    EXPIRED for every test in the suite. Reason: TestClient sends
    from `("testclient", 50000)` which `ipaddress.ip_address` rejects
    with ValueError, so `_is_private_or_loopback_ip` returns False
    -- which means EVERY test that POSTs /api/auth/set-password
    would 403 during the grace window (active for the first 30s of
    the process, which always covers the pytest run). Without this
    autouse fixture, test ordering decides whether you see the bug.

    Tests that want to exercise the grace explicitly (the four
    test_set_password_*_grace_* cases in test_auth.py) re-open the
    window in their own monkeypatch + force the IP check too."""
    from openmarquee import api_auth

    monkeypatch.setattr(
        api_auth,
        "_BOOT_MONOTONIC",
        api_auth.time.monotonic() - api_auth._SET_PASSWORD_GRACE_SECONDS - 1.0,
    )
