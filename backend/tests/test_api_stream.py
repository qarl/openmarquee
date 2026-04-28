"""Endpoint coverage for /api/stream/* (SYSTEM_SPEC §6 + §5.11).

Asserts the wire contract — request/response shapes, status codes,
overrides for active-session error reporting. The deeper behavioral
coverage (frame format, takeover semantics, pause+resume against the
loop) is in test_stream.py; here we just walk the four endpoints'
external contract.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
    _playback_loop_singleton,
    _stream_manager_singleton,
    get_playback_loop,
    get_stream_manager,
)
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer
from openmarquee.stream import StreamManager
from tests.test_stream import _FakeRTCPeerConnection


@pytest.fixture
def loop(tmp_path) -> PlaybackLoop:
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    return PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
    )


@pytest.fixture
def manager(loop: PlaybackLoop) -> StreamManager:
    return StreamManager(loop)


@pytest.fixture
def client(loop: PlaybackLoop, manager: StreamManager):
    app.dependency_overrides[get_playback_loop] = lambda: loop
    app.dependency_overrides[get_stream_manager] = lambda: manager
    try:
        with (
            patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection),
            TestClient(app) as test_client,
        ):
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _stream_manager_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()


def test_status_idle_when_no_session(client: TestClient):
    response = client.get("/api/stream/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "idle"
    assert body["session_id"] is None
    # Hardware tier is reported even when idle so the phone can clamp
    # getUserMedia constraints before its first /start.
    assert body["tier"]["name"] == "basic"
    assert body["tier"]["max_width"] == 854
    assert body["tier"]["max_height"] == 480
    assert body["tier"]["max_fps"] == 30


def test_start_returns_session_id_and_answer_sdp(client: TestClient):
    response = client.post(
        "/api/stream/start",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "session_id" in body
    assert "sdp_answer" in body
    assert body["sdp_answer"].startswith("v=0")


def test_status_reports_active_after_start(client: TestClient):
    start = client.post(
        "/api/stream/start",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    ).json()
    status = client.get("/api/stream/status").json()
    assert status["state"] == "active"
    assert status["session_id"] == start["session_id"]


def test_second_start_returns_409_with_active_session_id(client: TestClient):
    """Phone needs the active session id back so it can switch from
    'Go Live' to 'Take Over' without a separate /status round trip."""
    first = client.post(
        "/api/stream/start",
        json={"sdp_offer": "v=0\r\noffer-1\r\n"},
    ).json()

    second = client.post(
        "/api/stream/start",
        json={"sdp_offer": "v=0\r\noffer-2\r\n"},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "stream_already_active"
    assert detail["active_session_id"] == first["session_id"]


def test_stop_tears_down_session(client: TestClient):
    start = client.post(
        "/api/stream/start",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    ).json()

    stop = client.post(
        "/api/stream/stop",
        json={"session_id": start["session_id"]},
    )
    assert stop.status_code == 204

    status = client.get("/api/stream/status").json()
    assert status["state"] == "idle"
    assert status["session_id"] is None


def test_stop_unknown_session_returns_404(client: TestClient):
    from uuid import uuid4

    response = client.post(
        "/api/stream/stop",
        json={"session_id": str(uuid4())},
    )
    assert response.status_code == 404


def test_takeover_replaces_active_session(client: TestClient):
    first = client.post(
        "/api/stream/start",
        json={"sdp_offer": "v=0\r\noffer-1\r\n"},
    ).json()

    second = client.post(
        "/api/stream/takeover",
        json={"sdp_offer": "v=0\r\noffer-2\r\n"},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["session_id"] != first["session_id"]

    status = client.get("/api/stream/status").json()
    assert status["state"] == "active"
    assert status["session_id"] == second_body["session_id"]


def test_takeover_with_no_active_session_starts_one(client: TestClient):
    response = client.post(
        "/api/stream/takeover",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    )
    assert response.status_code == 200
    assert "session_id" in response.json()
