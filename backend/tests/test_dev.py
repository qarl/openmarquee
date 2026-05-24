from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _mock_renderer_singleton,
    get_content_storage,
    get_mock_renderer,
)
from openmarquee.rendering.mock import MockRenderer


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def renderer(tmp_path: Path) -> MockRenderer:
    return MockRenderer(8, 8, tmp_path / "preview.png")


@pytest.fixture
def client(storage: ContentStorage, renderer: MockRenderer) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_mock_renderer] = lambda: renderer
    try:
        # `with TestClient(app)` runs the lifespan context — matters because
        # the app's shutdown hook stops the playback loop cleanly.
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _mock_renderer_singleton.cache_clear()


# --- /dev/preview HTML page ---


def test_preview_page_serves_html(client: TestClient):
    response = client.get("/dev/preview")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text
    assert "/dev/preview/frame.png" in response.text


# --- /dev/preview/frame.png ---


def test_preview_frame_404_before_any_render(client: TestClient):
    response = client.get("/dev/preview/frame.png")
    assert response.status_code == 404


def test_preview_frame_returns_png_after_render(client: TestClient, renderer: MockRenderer):
    renderer.render_frame(bytes((255, 0, 0)) * (renderer.width * renderer.height))
    response = client.get("/dev/preview/frame.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


# POST /dev/play/{id} was retired alongside the playback engine
# (Phase 5+) — the route + its tests lived here through the early
# phases as the only way to push a stored content item through the
# MockRenderer; PlaybackLoop now drives that continuously off the
# default playlist. The /dev/preview surface (HTML page + frame.png)
# is still useful and tested above.


# ---- 2026-05-24: extended coverage for the dev-preview UX contract
# ---- (THIN tier closure). Pins behaviors a silent refactor could
# ---- break without test failure: the polling cadence, the cache-
# ---- buster query param, the 404 detail message, and the bytewise
# ---- integrity of the served frame.


def test_preview_page_includes_polling_javascript_setInterval_500ms(client: TestClient):
    """The page polls every 500ms via setInterval. Without this the
    page silently degrades to a static one-shot snapshot — the dev
    feedback loop (edit code → reload preview → see the new frame)
    depends entirely on the auto-refresh."""
    response = client.get("/dev/preview")
    assert response.status_code == 200
    assert "setInterval" in response.text, (
        "preview HTML must contain a setInterval call so the page "
        "auto-refreshes; without it the dev loop breaks silently"
    )
    assert "500" in response.text, (
        "polling interval should be 500ms; absent ms literal suggests "
        "the cadence was changed or the call was removed"
    )


def test_preview_page_polling_url_includes_cache_buster(client: TestClient):
    """The poll URL appends `?t=` + a timestamp so each request dodges
    the browser HTTP cache. Without the cache-buster the second poll
    onward returns the cached PNG and the page appears frozen — a
    refactor that drops the query param breaks the page without any
    server-side symptom."""
    response = client.get("/dev/preview")
    assert "?t=" in response.text, (
        "polling URL must include `?t=` cache-buster; otherwise the "
        "browser serves the cached PNG and the page appears frozen"
    )


def test_preview_frame_404_includes_operator_friendly_detail(client: TestClient):
    """The 404 detail is "no frame rendered yet" — an operator
    poking at the URL pre-render gets a meaningful explanation rather
    than just an opaque 404. Locks the message so a future refactor
    can't silently drop it."""
    response = client.get("/dev/preview/frame.png")
    assert response.status_code == 404
    body = response.json()
    assert body.get("detail") == "no frame rendered yet", (
        f"404 detail should be 'no frame rendered yet'; got {body!r}"
    )


def test_preview_frame_after_render_returns_exact_renderer_bytes(
    client: TestClient,
    renderer: MockRenderer,
):
    """The served PNG must be byte-identical to whatever the renderer
    most recently wrote — `FileResponse` streams from disk so any
    middleware that transformed the body (gzip-compress, re-encode,
    range-slice) would break the integrity. Tests `test_preview_
    frame_returns_png_after_render` above already checks magic bytes;
    this one pins the full content."""
    # Drive the renderer with a deterministic frame the mock encodes
    # to a stable PNG byte sequence.
    renderer.render_frame(bytes((255, 0, 0)) * (renderer.width * renderer.height))
    expected_bytes = renderer.output_path.read_bytes()
    response = client.get("/dev/preview/frame.png")
    assert response.status_code == 200
    assert response.content == expected_bytes, (
        "served PNG bytes must round-trip the renderer's output exactly "
        f"({len(response.content)} bytes served vs {len(expected_bytes)} expected)"
    )
