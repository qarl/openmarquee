"""Shared pytest fixtures for the Web slide helper tests.

The tests exercise the HTTP layer with the screenshot worker mocked --
no real browser is launched, so the suite runs on a host without
Playwright/Chromium installed.
"""

import pytest
from fastapi.testclient import TestClient

from openmarquee_web_helper import app as app_module

# A known token used across the auth tests.
TEST_TOKEN = "test-token-abc123"

# 1x1 transparent PNG -- stands in for "real" screenshot bytes.
CANNED_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000000020001e221bc330000000049454e44ae"
    "426082"
)


@pytest.fixture
def client(monkeypatch):
    """A TestClient with a fixed token and no real screenshot worker.

    The app's lifespan resolves the token from the env var; we set
    `OPENMARQUEE_WEB_HELPER_TOKEN` so no token file is touched.
    """
    monkeypatch.setenv("OPENMARQUEE_WEB_HELPER_TOKEN", TEST_TOKEN)
    # Entering the context manager runs the lifespan (token resolution).
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def restore_worker():
    """Restore the real worker indirection after each test that swaps it."""
    original = app_module.render_screenshot
    yield
    app_module.render_screenshot = original
