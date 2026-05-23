"""Endpoint coverage for /api/live/* (SYSTEM_SPEC §6 + §5.11).

Asserts the wire contract — request/response shapes, status codes,
overrides for active-session error reporting. The deeper behavioral
coverage (frame format, takeover semantics, pause+resume against the
loop) is in test_live.py; here we just walk the four endpoints'
external contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
    _live_manager_singleton,
    _playback_loop_singleton,
    get_live_manager,
    get_playback_loop,
)
from openmarquee.live import LiveManager
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer
from tests.test_live import _FakeRTCPeerConnection


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
def manager(loop: PlaybackLoop) -> LiveManager:
    return LiveManager(loop)


@pytest.fixture
def client(loop: PlaybackLoop, manager: LiveManager):
    app.dependency_overrides[get_playback_loop] = lambda: loop
    app.dependency_overrides[get_live_manager] = lambda: manager
    try:
        with (
            patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection),
            TestClient(app) as test_client,
        ):
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _live_manager_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()


def test_status_idle_when_no_session(client: TestClient):
    response = client.get("/api/live/status")
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
        "/api/live/start",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "session_id" in body
    assert "sdp_answer" in body
    assert body["sdp_answer"].startswith("v=0")


def test_status_reports_active_after_start(client: TestClient):
    start = client.post(
        "/api/live/start",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    ).json()
    status = client.get("/api/live/status").json()
    assert status["state"] == "active"
    assert status["session_id"] == start["session_id"]
    # Phase A.2: started_at flows through both /start and /status so
    # the phone's Elapsed counter ticks against the device's
    # authoritative start time. Both responses report the SAME wall-
    # clock UTC ISO 8601 timestamp for the same session — a panel
    # mounting mid-stream off /status sees the original /start time.
    assert "started_at" in start
    assert "started_at" in status
    assert status["started_at"] == start["started_at"]
    # ISO 8601 UTC: parseable + has timezone marker.
    from datetime import datetime

    parsed = datetime.fromisoformat(status["started_at"])
    assert parsed.tzinfo is not None


def test_status_started_at_is_none_when_idle(client: TestClient):
    """Phase A.2: /status returns started_at=null when no session is
    active, so the phone can distinguish 'no live session' from
    'live session, just no Elapsed yet'."""
    body = client.get("/api/live/status").json()
    assert body["state"] == "idle"
    assert body["started_at"] is None


def test_second_start_returns_409_with_active_session_id(client: TestClient):
    """Phone needs the active session id back so it can switch from
    'Go Live' to 'Take Over' without a separate /status round trip."""
    first = client.post(
        "/api/live/start",
        json={"sdp_offer": "v=0\r\noffer-1\r\n"},
    ).json()

    second = client.post(
        "/api/live/start",
        json={"sdp_offer": "v=0\r\noffer-2\r\n"},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "live_already_active"
    assert detail["active_session_id"] == first["session_id"]


def test_stop_tears_down_session(client: TestClient):
    start = client.post(
        "/api/live/start",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    ).json()

    stop = client.post(
        "/api/live/stop",
        json={"session_id": start["session_id"]},
    )
    assert stop.status_code == 204

    status = client.get("/api/live/status").json()
    assert status["state"] == "idle"
    assert status["session_id"] is None


def test_stop_unknown_session_returns_404(client: TestClient):
    from uuid import uuid4

    response = client.post(
        "/api/live/stop",
        json={"session_id": str(uuid4())},
    )
    assert response.status_code == 404


def test_takeover_replaces_active_session(client: TestClient):
    first = client.post(
        "/api/live/start",
        json={"sdp_offer": "v=0\r\noffer-1\r\n"},
    ).json()

    second = client.post(
        "/api/live/takeover",
        json={"sdp_offer": "v=0\r\noffer-2\r\n"},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["session_id"] != first["session_id"]

    status = client.get("/api/live/status").json()
    assert status["state"] == "active"
    assert status["session_id"] == second_body["session_id"]


def test_takeover_with_no_active_session_starts_one(client: TestClient):
    response = client.post(
        "/api/live/takeover",
        json={"sdp_offer": "v=0\r\noffer\r\n"},
    )
    assert response.status_code == 200
    assert "session_id" in response.json()


# --- Slice 3 (2026-05-23): structured 400 detail on negotiation failure -----
#
# Locks the new wire shape so a remote diagnoser sees the exception
# CLASS NAME (never the message). Regression for the netlink-EAFNOSUPPORT
# diagnosis cost: the prior opaque "live negotiation failed" string
# detail buried the real OSError [Errno 97] in the backend log only;
# `error_class` on the wire would have flagged "OSError" at the harness
# layer in seconds.


class _RaisingRTCPeerConnection:
    """Raises OSError from setLocalDescription — same exception class
    that aioice's gather raises on EAFNOSUPPORT (the FYS netlink-
    hardening bug). Used to drive api_live's exception handler into
    its sanitized-400 path under controlled conditions."""

    answer_sdp = "v=0\r\nfake-answer\r\n"

    def __init__(self):
        self.handlers: dict[str, object] = {}
        self.closed = False

    def on(self, event: str):
        def decorator(fn):
            self.handlers[event] = fn
            return fn

        return decorator

    async def setRemoteDescription(self, desc):  # noqa: ANN001
        pass

    async def createAnswer(self):
        class _Desc:
            sdp = self.answer_sdp
            type = "answer"

        return _Desc()

    async def setLocalDescription(self, desc):  # noqa: ANN001
        raise OSError(97, "Address family not supported by protocol")

    async def close(self):
        self.closed = True


def test_start_400_surfaces_error_class_in_detail(client: TestClient):
    """The 400 detail must be a structured dict carrying the exception
    CLASS NAME — never the exception message. Lock this regression
    pre-deploy so a future systemd / aiortc / SDP-parse failure doesn't
    again cost 25 min of journalctl spelunking to identify."""
    with patch("openmarquee.live.RTCPeerConnection", _RaisingRTCPeerConnection):
        response = client.post(
            "/api/live/start",
            json={"sdp_offer": "v=0\r\noffer\r\n"},
        )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, dict), f"detail must be a dict; got {detail!r}"
    assert detail["error"] == "live_negotiation_failed"
    # error_class is the type-name string; assert it's present + a
    # non-empty Python identifier rather than pinning a specific class
    # (the catch is broad — many exception types can reach it).
    error_class = detail["error_class"]
    assert isinstance(error_class, str) and error_class
    assert error_class.isidentifier()
    # On the wire we MUST NOT leak the exception message (it can carry
    # paths / internals). The _RaisingRTCPeerConnection raises with
    # "Address family not supported by protocol" — that string must
    # NOT appear anywhere in the response body.
    body_text = response.text
    assert "Address family not supported" not in body_text


def test_takeover_400_surfaces_error_class_in_detail(client: TestClient):
    """Symmetric assertion for the /takeover handler. Same wire shape;
    error code is `live_takeover_failed` to distinguish the surface."""
    with patch("openmarquee.live.RTCPeerConnection", _RaisingRTCPeerConnection):
        response = client.post(
            "/api/live/takeover",
            json={"sdp_offer": "v=0\r\noffer\r\n"},
        )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error"] == "live_takeover_failed"
    error_class = detail["error_class"]
    assert isinstance(error_class, str) and error_class
    assert error_class.isidentifier()
    assert "Address family not supported" not in response.text


# --- Slice 4 Test B (2026-05-23): real aiortc client SDP round-trip ---------
#
# Happy-path lock for the Live signaling path. Drives a REAL aiortc
# RTCPeerConnection (not _FakeRTCPeerConnection) through
# /api/live/start against the in-process app with real aiortc on the
# server side too. Catches general aiortc/SDP regressions that the
# fake-PC tests can't see (codec negotiation, ICE candidate handling,
# real SDP parser behavior).
#
# Skip-gated on aiortc availability — the rest of the test surface
# uses _FakeRTCPeerConnection and would skip cleanly if a runner
# doesn't have aiortc installed. We DON'T skip on macOS: the
# happy-path doesn't depend on netlink (Mac's getifaddrs uses
# sysctl, not netlink), so the test passes on both platforms and
# the Mac dev-loop benefits from the coverage. QA's earlier
# "skip on darwin" guidance was paired with the primary
# (seccomp-hardening) approach which we DEFERRED; the fallback
# this test implements has no Mac-specific failure mode.
#
# Note: this test exercises SDP exchange + answer-parsing on both
# sides (client applies the answer at the end — catches answer-
# parser regressions too). It does NOT poll for
# pc.connectionState == "connected" (that would test DTLS / SRTP /
# ICE-connectivity on the loopback transport, which is outside the
# netlink-regression target). It does NOT send frames. The
# end-to-end frame-flow case is manually validated against FYS
# (the Slice 2 shake-out + the backend log correlation showing
# 70 frames painted).


@pytest.mark.asyncio
async def test_aiortc_client_round_trips_real_sdp_through_api_live_start(
    loop: PlaybackLoop, manager: LiveManager
):
    """Real aiortc RTCPeerConnection on the client side POSTs a real
    SDP offer (not the "v=0\\r\\noffer" stub the other tests use) to
    /api/live/start. The server runs real aiortc too — no
    _FakeRTCPeerConnection patch — so the test exercises actual
    `setRemoteDescription` + `createAnswer` + `setLocalDescription`
    (with ICE gathering) on the production code path.

    Asserts:
    - 200 OK.
    - response carries `session_id` + `sdp_answer` + `started_at`.
    - `sdp_answer` is a parseable SDP (starts with v=0) and aiortc
      client-side can apply it via setRemoteDescription without
      raising.
    - /api/live/stop returns 204 + status flips back to idle.

    Would have caught the FYS netlink-EAFNOSUPPORT regression
    immediately if the test runner enforced systemd-style
    RestrictAddressFamilies (the primary-B approach we DEFERRED —
    see Slice 4 commit message). With Slice 4 Test A's static
    config lock + this happy-path lock + Slice 3's _RaisingRTC
    exception-shape lock, the three-axis coverage matrix is
    ~complete.
    """
    pytest.importorskip("aiortc")
    pytest.importorskip("httpx")
    import httpx
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.contrib.media import MediaPlayer

    fixture_mp4 = Path(__file__).resolve().parent.parent.parent / "ui" / "test" / "fixture.mp4"
    if not fixture_mp4.is_file():
        pytest.skip(f"harness fixture missing at {fixture_mp4}")

    # Wire the test app to the loop + manager fixtures, NO patch on
    # RTCPeerConnection — the production aiortc class is what gets
    # exercised here.
    app.dependency_overrides[get_playback_loop] = lambda: loop
    app.dependency_overrides[get_live_manager] = lambda: manager
    pc: RTCPeerConnection | None = None
    player: MediaPlayer | None = None
    try:
        # Build the client-side PC + load the looping fixture as the
        # video track source.
        pc = RTCPeerConnection()
        player = MediaPlayer(str(fixture_mp4), loop=True)
        assert player.video is not None, (
            "MediaPlayer must yield a video track from the harness fixture"
        )
        pc.addTrack(player.video)

        # Non-trickle ICE: bake all candidates into the local
        # description before sending the offer. Same shape the
        # browser harness's `waitForIceGatheringComplete` enforces.
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        # Bound the gather wait so a misconfigured runner can't hang
        # the test indefinitely. 30 s is generous; typical local
        # gather completes in <5 s.
        for _ in range(600):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.05)
        assert pc.iceGatheringState == "complete", (
            f"iceGatheringState stuck at {pc.iceGatheringState} after 30 s; "
            f"runner may lack a usable network interface (or netlink, on Linux)"
        )

        # POST the offer via httpx ASGI transport so the request goes
        # through the FastAPI app in-process WITHOUT a TestClient
        # thread context — keeps everything in the test's asyncio
        # loop so the production aiortc handler shares the loop with
        # the client PC.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            start_resp = await http.post(
                "/api/live/start",
                json={"sdp_offer": pc.localDescription.sdp},
                timeout=60.0,
            )
            assert start_resp.status_code == 200, (
                f"expected 200, got {start_resp.status_code}: {start_resp.text}"
            )
            body = start_resp.json()
            assert "session_id" in body
            assert "started_at" in body
            assert isinstance(body["sdp_answer"], str)
            assert body["sdp_answer"].startswith("v=0"), (
                f"sdp_answer doesn't look like SDP: {body['sdp_answer'][:80]!r}"
            )
            # Client-side applies the answer — exercises aiortc's SDP
            # parser on the answer the server just produced. If the
            # answer is malformed in some way the fake test couldn't
            # see, this raises.
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=body["sdp_answer"], type="answer")
            )

            # Stop cleanly to leave the live manager idle for any
            # following test.
            stop_resp = await http.post(
                "/api/live/stop",
                json={"session_id": body["session_id"]},
                timeout=10.0,
            )
            assert stop_resp.status_code == 204
            status_resp = await http.get("/api/live/status")
            assert status_resp.status_code == 200
            assert status_resp.json()["state"] == "idle"
    finally:
        if pc is not None:
            # Best-effort cleanup — pc.close() can raise if the connection
            # is already half-torn-down; we don't care here.
            import contextlib

            with contextlib.suppress(Exception):
                await pc.close()
        app.dependency_overrides.clear()
        _live_manager_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()


# --- Slice 4 Test A (2026-05-23): canonical systemd unit AF_NETLINK lock ----
#
# Catches the regression on every CI run regardless of platform —
# pure file-parse, no aiortc / no Linux runtime dependency. If a
# future "harden the unit further" PR strips AF_NETLINK without
# understanding aioice's ICE-gather dependency, CI fails here
# immediately with a pointer at the comment block that explains why.


def test_systemd_unit_whitelists_af_netlink():
    """The canonical openmarquee-backend.service must include
    AF_NETLINK in RestrictAddressFamilies. aiortc's aioice opens an
    AF_NETLINK socket to enumerate interfaces during ICE candidate
    gathering; without the whitelist entry, `/api/live/start` returns
    400 with OSError [Errno 97] (Address family not supported by
    protocol). Diagnosed on FYS 2026-05-23.
    """
    unit = Path(__file__).resolve().parent.parent.parent / "system" / "openmarquee-backend.service"
    assert unit.is_file(), (
        f"canonical unit file not found at {unit}; relocation? update the test path."
    )
    text = unit.read_text()
    line = next(
        (ln for ln in text.splitlines() if ln.strip().startswith("RestrictAddressFamilies=")),
        None,
    )
    assert line is not None, (
        "RestrictAddressFamilies= directive missing from "
        f"{unit}. The systemd hardening line must remain present + carry "
        "AF_NETLINK."
    )
    assert "AF_NETLINK" in line, (
        f"AF_NETLINK missing from RestrictAddressFamilies — aiortc/aioice "
        f"needs it for ICE candidate gathering (see the comment block above "
        f"the directive in {unit.name} + the FYS 2026-05-23 diagnosis in "
        f"the Slice 3 commit). Stripping it silently re-introduces the "
        f"Mode A '/api/live/start 400' bug. Current line: {line!r}"
    )


# --- STREAM/VLC slice 3: tier table ---------------------------------------


def test_hardware_tier_json_round_trips():
    """HardwareTier survives a model_dump -> model_validate round trip
    for both the basic and good tiers — the /status wire shape and any
    persisted form stay stable."""
    from openmarquee.api_live import HardwareTier

    for tier in (
        HardwareTier(name="basic", max_width=854, max_height=480, max_fps=30),
        HardwareTier(name="good", max_width=1920, max_height=1080, max_fps=30),
    ):
        restored = HardwareTier.model_validate(tier.model_dump())
        assert restored == tier


def test_good_tier_is_pi4_1080p():
    """The `good` tier (Pi 4/5) is 1920×1080/30 per STREAM_VLC §7."""
    from openmarquee.api_live import _GOOD_TIER

    assert _GOOD_TIER.name == "good"
    assert (_GOOD_TIER.max_width, _GOOD_TIER.max_height) == (1920, 1080)
    assert _GOOD_TIER.max_fps == 30


def test_source_tier_table_covers_both_sources():
    """The per-source tier table has an entry for each stream source
    (webrtc + rtsp); both are basic today."""
    from openmarquee.api_live import _BASIC_TIER, _SOURCE_TIERS

    assert set(_SOURCE_TIERS) == {"webrtc", "stream"}
    assert all(tier == _BASIC_TIER for tier in _SOURCE_TIERS.values())


def test_status_tier_shape_is_complete(client: TestClient):
    """/status reports a fully-formed tier object (name + all three
    caps) — the phone reads every field to clamp its capture."""
    body = client.get("/api/live/status").json()
    tier = body["tier"]
    assert set(tier) == {"name", "max_width", "max_height", "max_fps"}
    assert tier["name"] in ("basic", "good", "future")
    assert all(isinstance(tier[k], int) for k in ("max_width", "max_height", "max_fps"))


# --- STREAM/VLC slice 4: stream takeover via the start-request union ------


def test_start_legacy_body_without_kind_still_works(client: TestClient):
    """A start body with no `kind` ({"sdp_offer": ...}) still validates
    as a WebRTC start — the deployed phone client predates the stream
    work and must not break."""
    response = client.post("/api/live/start", json={"sdp_offer": "v=0\r\noffer\r\n"})
    assert response.status_code == 200
    assert response.json()["sdp_answer"]  # WebRTC start has an answer


def test_start_stream_returns_session_without_sdp_answer(client: TestClient, monkeypatch, tmp_path):
    """POST /start with a kind=stream body starts a stream takeover and
    returns a session whose sdp_answer is null (a stream has no SDP)."""
    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from tests.test_stream_consumer import _write_mock_ffmpeg

    mock = _write_mock_ffmpeg(tmp_path / "ffmpeg", frame_size=8 * 8 * 3, n_frames=2)
    monkeypatch.setattr(
        "openmarquee.stream_source.StreamConsumer",
        functools.partial(StreamConsumer, ffmpeg_bin=mock),
    )

    response = client.post(
        "/api/live/start",
        json={"kind": "stream", "url": "rtsp://laptop:8554/live"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "session_id" in body
    assert body["sdp_answer"] is None
    assert client.get("/api/live/status").json()["state"] == "active"
    # Stop the session so the real ffmpeg subprocess is reaped inside
    # this test's event loop (the WebRTC tests use a fake PC and have
    # no subprocess to clean up).
    stop = client.post("/api/live/stop", json={"session_id": body["session_id"]})
    assert stop.status_code == 204
