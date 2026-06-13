"""r64 (2026-06-05): integration tests for GET /api/perf-stats.

Pins:
  * The endpoint exists, no auth, returns the documented schema.
  * Recorded data flows into the response.
  * The endpoint is cheap (no syscalls, no IO during the request).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _mock_renderer_singleton,
    _playback_loop_singleton,
    get_content_storage,
    get_mock_renderer,
    get_playback_loop,
)
from openmarquee.perf_stats import PerfStats
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def renderer(tmp_path: Path) -> MockRenderer:
    return MockRenderer(8, 8, tmp_path / "preview.png")


@pytest.fixture
def loop(storage: ContentStorage, renderer: MockRenderer) -> PlaybackLoop:
    return PlaybackLoop(
        renderer=renderer,
        fetch_items=storage.list_all,
        read_asset=storage.read_asset,
        empty_playlist_poll_seconds=0.01,
    )


@pytest.fixture
def client(
    storage: ContentStorage,
    renderer: MockRenderer,
    loop: PlaybackLoop,
    monkeypatch: pytest.MonkeyPatch,
):
    # Avoid the lifespan worker pile starting CMA sampler etc.
    monkeypatch.setenv("OPENMARQUEE_DISABLE_AUTOSTART", "1")
    monkeypatch.setenv("OPENMARQUEE_DISABLE_PULL_WORKER", "1")
    monkeypatch.setenv("OPENMARQUEE_DISABLE_SEED", "1")
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_mock_renderer] = lambda: renderer
    app.dependency_overrides[get_playback_loop] = lambda: loop
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _mock_renderer_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()


def test_perf_stats_endpoint_returns_schema(client: TestClient) -> None:
    """Endpoint exists, returns 200, no auth required."""
    resp = client.get("/api/perf-stats")
    assert resp.status_code == 200
    body = resp.json()
    # Schema keys per dispatch:
    for key in (
        "timestamp",
        "uptime_s",
        "frames_observed_total",
        "frame_over_budget_total",
        "transitions_total",
        "cma_used_max_mb_1m",
        "cma_used_avg_mb_1m",
        "preload_recent",
        "first_frame_paint_recent",
        "v4l2_prime_recent",
        "video_load_recent",
        "frame_over_budget_recent",
        "begin_slide_load_recent",
        "begin_transition_load_recent",
    ):
        assert key in body, f"missing schema key: {key}"
    # Empty buffers for a freshly-started backend (no preloads fired).
    assert isinstance(body["preload_recent"], list)
    assert isinstance(body["frames_observed_total"], int)


def test_perf_stats_no_auth_required(client: TestClient) -> None:
    """No Authorization header → still 200. The dispatch is
    explicit: 'NOT require auth (read-only, localhost-OK)'."""
    resp = client.get("/api/perf-stats", headers={})
    assert resp.status_code == 200


def test_perf_stats_includes_pushed_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Push a record + verify it appears in the endpoint response.
    Patches the module-level STATS so the assertion isn't polluted
    by other tests' state."""
    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    # Also patch the endpoint's import of STATS (it imports at
    # module load time, before our monkeypatch fires here).
    monkeypatch.setattr("openmarquee.api_perf.STATS", fresh)
    fresh.record_preload("test-slide", 123456)
    fresh.record_transition_observation()
    fresh.record_frame_observation(delta_ms=200, in_transition=False)

    resp = client.get("/api/perf-stats")
    body = resp.json()
    assert body["transitions_total"] == 1
    assert body["frame_over_budget_total"] == 1
    assert body["frames_observed_total"] == 1
    assert len(body["preload_recent"]) == 1
    assert body["preload_recent"][0]["slide_id"] == "test-slide"
    assert body["preload_recent"][0]["us"] == 123456


def test_perf_stats_serve_is_cheap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Single serve is sub-50ms even with a full rolling buffer.

    The dispatch requires 'single-digit milliseconds' but the
    pytest+FastAPI test-client overhead inflates this on the dev
    box; 50ms is a generous ceiling that still catches a
    regression like adding a blocking IO call into the handler."""
    import time as _time

    fresh = PerfStats()
    monkeypatch.setattr("openmarquee.perf_stats.STATS", fresh)
    monkeypatch.setattr("openmarquee.api_perf.STATS", fresh)
    # Fill all buffers to their max.
    for i in range(60):
        fresh.record_preload(f"s-{i}", i * 100)
        fresh.record_first_frame_paint(f"s-{i}", 1000, 0, 100, 33000, 35000, cache_hit=(i % 2 == 0))
        fresh.record_frame_observation(delta_ms=200, in_transition=False)
    start = _time.perf_counter()
    resp = client.get("/api/perf-stats")
    elapsed_ms = (_time.perf_counter() - start) * 1000
    assert resp.status_code == 200
    assert elapsed_ms < 50, f"endpoint took {elapsed_ms:.1f}ms (budget 50ms)"
