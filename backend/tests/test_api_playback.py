import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openmarquee.app import app
from openmarquee.content import TextSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _mock_renderer_singleton,
    _playback_loop_singleton,
    get_content_storage,
    get_mock_renderer,
    get_playback_loop,
)
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
def client(storage: ContentStorage, renderer: MockRenderer, loop: PlaybackLoop) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_mock_renderer] = lambda: renderer
    app.dependency_overrides[get_playback_loop] = lambda: loop
    try:
        # `with TestClient(app)` triggers the app's lifespan context — needed
        # here because the lifespan shutdown calls get_playback_loop().stop(),
        # which (via the override) unwinds the fixture's loop cleanly.
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _mock_renderer_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()


def test_state_returns_not_running_initially(client: TestClient):
    response = client.get("/api/playback/state")
    assert response.status_code == 200
    assert response.json() == {
        "is_running": False,
        "current_item_id": None,
        "current_item_type": None,
        "current_item_transition": None,
        "current_item_transition_ms": None,
        "current_item_auto_mode": None,
        "current_item_auto_format": None,
        "current_playlist_id": None,
    }


def test_start_returns_204_and_state_flips_to_running(client: TestClient):
    response = client.post("/api/playback/start")
    assert response.status_code == 204
    # Check via the state endpoint — it runs on the same event loop as the
    # playback task, so it sees the true state (cross-thread attribute peeks
    # from pytest's main thread can race the portal's loop).
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is True
    client.post("/api/playback/stop")


def test_stop_returns_204_and_state_flips_to_not_running(client: TestClient):
    client.post("/api/playback/start")
    response = client.post("/api/playback/stop")
    assert response.status_code == 204
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is False
    assert state["current_item_id"] is None


def test_start_then_start_is_idempotent(client: TestClient):
    client.post("/api/playback/start")
    response = client.post("/api/playback/start")
    assert response.status_code == 204
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is True
    client.post("/api/playback/stop")


def test_stop_when_not_running_is_idempotent(client: TestClient):
    response = client.post("/api/playback/stop")
    assert response.status_code == 204
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is False


def test_state_reports_current_item_id_while_playing(client: TestClient, storage: ContentStorage):
    """With a real item in storage, start the loop and poll state — the
    backend should surface the currently-rendering slide's id + type."""
    slide = TextSlide(name="x", text="x", duration_ms=1000)
    storage.save_text_slide(slide, _png_bytes(8, 8, (255, 0, 0)))

    client.post("/api/playback/start")

    # Poll for up to 2s while the portal's event loop schedules the task.
    deadline = time.time() + 2.0
    state = {}
    while time.time() < deadline:
        state = client.get("/api/playback/state").json()
        if state.get("current_item_id") is not None:
            break
        time.sleep(0.05)

    assert state["current_item_id"] == str(slide.id)
    # The live-preview UI picks <video> vs <img> off this field, so it
    # must track the item type exactly — "text_slide" here.
    assert state["current_item_type"] == "text_slide"
    client.post("/api/playback/stop")


def test_current_thumbnail_returns_204_when_idle(client: TestClient):
    # Nothing playing → 204, so the Flock tile can cheaply distinguish
    # "idle" from "offline" without parsing a 5xx.
    response = client.get("/api/playback/current-thumbnail")
    assert response.status_code == 204


def test_current_thumbnail_serves_current_item_asset(client: TestClient, storage: ContentStorage):
    slide = TextSlide(name="thumb-test", text="t", duration_ms=1000)
    png = _png_bytes(8, 8, (0, 200, 100))
    storage.save_text_slide(slide, png)

    client.post("/api/playback/start")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if client.get("/api/playback/state").json().get("current_item_id"):
            break
        time.sleep(0.05)

    response = client.get("/api/playback/current-thumbnail")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    # Batch 11.3 / sweep #5 #4: CORS is no longer a wildcard. Without an
    # Origin header (same-origin request), the response carries NO
    # access-control-allow-origin header at all -- browser doesn't need
    # one for same-origin reads. Flock-allowlisted cross-origin coverage
    # lives in test_current_thumbnail_cors_allowlist_*.
    assert "access-control-allow-origin" not in response.headers
    assert response.headers.get("cache-control") == "no-store"
    assert response.content == png
    client.post("/api/playback/stop")


def test_current_thumbnail_returns_first_item_of_current_playlist(tmp_path: Path):
    """The thumbnail is the playlist's cover (first slide), not the
    currently-rotating slide. Set up a loop that stamps
    `current_playlist_id` on its items so the endpoint walks the
    playlist rather than the fallback path."""
    from openmarquee.dependencies import (
        _content_storage_singleton,
        _playback_loop_singleton,
        _playlist_storage_singleton,
        get_content_storage,
        get_playback_loop,
        get_playlist_storage,
    )
    from openmarquee.playlist import (
        DEFAULT_PLAYLIST_ID,
        Playlist,
        PlaylistItem,
        PlaylistStorage,
    )
    from openmarquee.rendering.mock import MockRenderer

    content = ContentStorage(tmp_path / "content")
    playlists = PlaylistStorage(tmp_path / "playlist.json")
    renderer = MockRenderer(8, 8, tmp_path / "preview.png")

    # Two distinct slides so "first" vs "current" is observable.
    first = TextSlide(name="first", text="A", duration_ms=100)
    second = TextSlide(name="second", text="B", duration_ms=100)
    first_png = _png_bytes(8, 8, (200, 100, 0))
    second_png = _png_bytes(8, 8, (0, 100, 200))
    content.save_text_slide(first, first_png)
    content.save_text_slide(second, second_png)
    playlists.set_by_id(
        Playlist(
            id=DEFAULT_PLAYLIST_ID,
            name="default",
            items=[
                PlaylistItem(item_id=first.id),
                PlaylistItem(item_id=second.id),
            ],
        )
    )

    from openmarquee.playback import PlaybackLoop

    loop_holder: dict = {}

    def fetch():
        # Mirror scheduled_fetch_items — stamp the playlist id so the
        # current-thumbnail endpoint can find the playlist's first item.
        loop_holder["loop"]._stamp_playlist_id(DEFAULT_PLAYLIST_ID)
        default_pl = playlists.get_by_id(DEFAULT_PLAYLIST_ID)
        return [content.load(iid) for iid in default_pl.item_ids]

    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=fetch,
        read_asset=content.read_asset,
        empty_playlist_poll_seconds=0.01,
    )
    loop_holder["loop"] = loop

    app.dependency_overrides[get_content_storage] = lambda: content
    app.dependency_overrides[get_playback_loop] = lambda: loop
    app.dependency_overrides[get_playlist_storage] = lambda: playlists
    try:
        with TestClient(app) as client:
            client.post("/api/playback/start")
            # Wait through at least one slide-advance so the loop is on the
            # SECOND item — proves the thumbnail still returns the FIRST.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                cid = client.get("/api/playback/state").json().get("current_item_id")
                if cid == str(second.id):
                    break
                time.sleep(0.05)
            response = client.get("/api/playback/current-thumbnail")
            assert response.status_code == 200
            assert response.content == first_png
            client.post("/api/playback/stop")
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()
        _playlist_storage_singleton.cache_clear()


# --- Batch 11.3 / sweep #5 #4: CORS allowlist tests ---


def _playing_text_slide(client: TestClient, storage: ContentStorage) -> None:
    """Helper: seed + start playback so /current-thumbnail returns 200."""
    slide = TextSlide(name="cors-test", text="t", duration_ms=1000)
    png = _png_bytes(8, 8, (0, 200, 100))
    storage.save_text_slide(slide, png)
    client.post("/api/playback/start")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if client.get("/api/playback/state").json().get("current_item_id"):
            return
        time.sleep(0.05)


def test_current_thumbnail_no_cors_for_unknown_origin(client: TestClient, storage: ContentStorage):
    """A cross-origin GET from a domain NOT on the flock allowlist
    gets no Access-Control-Allow-Origin -- browser blocks the read."""
    _playing_text_slide(client, storage)
    response = client.get(
        "/api/playback/current-thumbnail",
        headers={"Origin": "https://attacker.example.com"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    client.post("/api/playback/stop")


def test_current_thumbnail_cors_for_localhost_origin(client: TestClient, storage: ContentStorage):
    """localhost is in the builtin allowlist (dev convenience)."""
    _playing_text_slide(client, storage)
    response = client.get(
        "/api/playback/current-thumbnail",
        headers={"Origin": "http://localhost:8000"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ("http://localhost:8000")
    assert response.headers.get("vary") == "Origin"
    client.post("/api/playback/stop")


def test_current_thumbnail_cors_for_flock_peer_origin(
    client: TestClient, storage: ContentStorage, tmp_path: Path
):
    """A peer in the operator's flock gets reflective allow. Set up a
    FlockStorage override with one peer, then probe with that origin."""
    from openmarquee.dependencies import (
        _flock_storage_singleton,
        get_flock_storage,
    )
    from openmarquee.flock import Flock, FlockPeer, FlockStorage

    flock_path = tmp_path / "flock.json"
    flock_storage = FlockStorage(flock_path)
    flock_storage.save(Flock(peers=[FlockPeer(address="peer-lobby.ts.net")]))
    app.dependency_overrides[get_flock_storage] = lambda: flock_storage
    _flock_storage_singleton.cache_clear()
    try:
        _playing_text_slide(client, storage)
        response = client.get(
            "/api/playback/current-thumbnail",
            headers={"Origin": "https://peer-lobby.ts.net"},
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == ("https://peer-lobby.ts.net")
        assert response.headers.get("vary") == "Origin"
    finally:
        client.post("/api/playback/stop")
        _flock_storage_singleton.cache_clear()


# Perf-night r2 (2026-05-26): /api/playback/perf/stats sidecar read.
# These tests pin the contract between the renderer's PerfStatsJson
# (renderer/src/ipc_main.rs) and the backend's RendererPerfStats wire
# model. A breaking rename on either side trips these.


def _valid_perf_stats_payload() -> dict:
    """Canonical fixture mirroring renderer/src/ipc_main.rs
    PerfStatsJson. Update both sides in lockstep when the wire model
    grows new fields."""
    return {
        "window_s": 30,
        "frames": 900,
        "transitions": 50,
        "fps_avg": 29.8,
        "paint_us_avg": 5000,
        "paint_us_max": 33000,
        "paint_us_p99": 28000,
        "session_frames": 12000,
        "session_transitions": 600,
        "frames_observed_total": 18000,
        "frames_over_budget_total": 234,
        "timestamp_unix_s": 1748275200,
    }


def test_perf_stats_returns_503_when_sidecar_missing(
    client: TestClient, tmp_path: Path, monkeypatch
):
    """First 30s of any session: renderer hasn't emitted an ipc.soak
    window yet, so the sidecar file doesn't exist. Operator-facing
    overlay must see 503 + a stable detail string so the UI can show
    a 'no data yet' state cleanly."""
    monkeypatch.setenv("OPENMARQUEE_PERF_STATS_PATH", str(tmp_path / "missing.json"))
    response = client.get("/api/playback/perf/stats")
    assert response.status_code == 503
    assert "not yet written" in response.json()["detail"]


def test_perf_stats_returns_parsed_json_when_sidecar_valid(
    client: TestClient, tmp_path: Path, monkeypatch
):
    """End-to-end wire-contract: renderer writes the canonical
    PerfStatsJson, backend parses it, response carries every field
    through unchanged."""
    import json as _json

    perf_path = tmp_path / "perf-stats.json"
    payload = _valid_perf_stats_payload()
    perf_path.write_text(_json.dumps(payload))
    monkeypatch.setenv("OPENMARQUEE_PERF_STATS_PATH", str(perf_path))

    response = client.get("/api/playback/perf/stats")
    assert response.status_code == 200
    body = response.json()
    assert body == payload  # full round-trip; every field preserved.


def test_perf_stats_returns_503_on_malformed_json(
    client: TestClient, tmp_path: Path, monkeypatch
):
    """Defense in depth: the renderer uses .tmp+rename atomic writes
    so a torn write shouldn't reach the backend — but if it ever does
    (NFS, disk-full mid-rename, manual corruption), we want a 503 with
    a 'parse failed' detail rather than a 500 stack trace."""
    perf_path = tmp_path / "perf-stats.json"
    perf_path.write_text("{not valid json")
    monkeypatch.setenv("OPENMARQUEE_PERF_STATS_PATH", str(perf_path))

    response = client.get("/api/playback/perf/stats")
    assert response.status_code == 503
    assert "parse failed" in response.json()["detail"]


def test_perf_stats_returns_503_on_schema_mismatch(
    client: TestClient, tmp_path: Path, monkeypatch
):
    """Schema-drift guard: if the renderer side renames a field
    without the backend catching up, Pydantic ValidationError surfaces
    as a 503 + 'schema mismatch' detail (NOT a 500). Operator can
    spot the drift in the UI overlay's error state and the field name
    is in the journal."""
    import json as _json

    perf_path = tmp_path / "perf-stats.json"
    payload = _valid_perf_stats_payload()
    del payload["frames_over_budget_total"]  # simulate renderer rename
    perf_path.write_text(_json.dumps(payload))
    monkeypatch.setenv("OPENMARQUEE_PERF_STATS_PATH", str(perf_path))

    response = client.get("/api/playback/perf/stats")
    assert response.status_code == 503
    assert "schema mismatch" in response.json()["detail"]


# Perf-night r3 (2026-05-26): /api/playback/loop_stats endpoint.
# Pins the wire contract between PlaybackLoop.get_loop_stats() and the
# Pydantic PythonLoopStats response model.


def test_loop_stats_returns_all_zero_for_fresh_loop(
    client: TestClient, loop
):
    """At test fixture setup, the PlaybackLoop has no ticks recorded
    (the loop hasn't been started + no slide has played yet). The
    endpoint must return all-zero rather than 4xx/5xx so the operator
    UI can render a clean 'no data yet' state."""
    response = client.get("/api/playback/loop_stats")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ticks_observed": 0,
        "p50_us": 0,
        "p95_us": 0,
        "p99_us": 0,
        "max_us": 0,
        "ticks_over_budget": 0,
    }


def test_loop_stats_reflects_recorded_ticks(
    client: TestClient, loop
):
    """Drop synthetic ticks into the loop's ring buffer + verify the
    endpoint surfaces them with the documented percentile math (matches
    renderer/src/profile.rs:summarize_samples indexing). 99 fast ticks
    + 1 spike: p50/p95 = fast, p99/max = spike, over_budget = 1."""
    from uuid import uuid4

    slide_id = uuid4()
    for _ in range(99):
        loop._record_tick(1_000_000, slide_id, "advance")  # 1ms
    loop._record_tick(50_000_000, slide_id, "advance")  # 50ms — over budget

    response = client.get("/api/playback/loop_stats")
    assert response.status_code == 200
    body = response.json()
    assert body["ticks_observed"] == 100
    assert body["p50_us"] == 1000
    assert body["p95_us"] == 1000
    assert body["p99_us"] == 50_000
    assert body["max_us"] == 50_000
    assert body["ticks_over_budget"] == 1
