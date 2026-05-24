"""Tests for the Batch 6.1 perf-stats endpoint + ASGI middleware.

The /api/system/perf-stats endpoint aggregates class-level counters
on every Storage class plus the text_raster LRU font cache info.
The middleware additionally maintains an in-memory ring of recent
requests; the endpoint surfaces that ring as `request_log` so the
sweep baseline capture can correlate counters with the routes that
drove them.

These tests pin:
  * the endpoint returns the documented shape
  * counters increment when their guarded method is called
  * the middleware records request entries with method/path/status
    /duration_ms keys
  * the recent-requests ring is bounded (deque maxlen)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _playlist_storage_singleton,
    get_content_storage,
    get_playlist_storage,
)
from openmarquee.flock import FlockStorage
from openmarquee.perf_middleware import (
    _REQUEST_LOG_MAX,
    _coerce_request_id,
    clear_request_log,
    recent_requests,
)
from openmarquee.playlist import PlaylistStorage
from openmarquee.schedule import ScheduleStorage
from openmarquee.settings import SettingsStorage
from openmarquee.text_raster import clear_font_cache


@pytest.fixture(autouse=True)
def _isolate_perf_state():
    """Reset class-level counters + the request ring around every
    test. Without this the class-level counters bleed across tests
    in unpredictable order."""
    for cls in (
        ContentStorage,
        PlaylistStorage,
        FlockStorage,
        SettingsStorage,
        ScheduleStorage,
    ):
        for k in list(cls._stats):
            cls._stats[k] = 0
    clear_request_log()
    clear_font_cache()
    yield


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """TestClient with isolated content/playlist storage so the
    counter assertions don't depend on dev-machine state."""
    content_storage = ContentStorage(tmp_path / "content")
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    app.dependency_overrides[get_content_storage] = lambda: content_storage
    app.dependency_overrides[get_playlist_storage] = lambda: playlist_storage
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        _playlist_storage_singleton.cache_clear()


def test_perf_stats_returns_expected_shape(client: TestClient):
    """The endpoint shape -- if a key drifts, the baseline-capture
    script will silently lose a counter. Pin every section."""
    response = client.get("/api/system/perf-stats")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) >= {
        "content_storage",
        "playlist_storage",
        "flock_storage",
        "settings_storage",
        "schedule_storage",
        "font_cache",
        "request_log",
    }
    # Font cache info section.
    assert {"hits", "misses", "maxsize", "currsize"} <= set(body["font_cache"])
    # Counter dicts are dicts of int.
    for section in (
        "content_storage",
        "playlist_storage",
        "flock_storage",
        "settings_storage",
        "schedule_storage",
    ):
        assert all(isinstance(v, int) for v in body[section].values())


def test_content_storage_list_all_counter_increments(client: TestClient):
    """list_all_calls -- the headline counter sweep #2 wants to
    measure. /api/content GETs route through list_all; each call
    should bump."""
    before = client.get("/api/system/perf-stats").json()["content_storage"]
    client.get("/api/content")
    client.get("/api/content")
    after = client.get("/api/system/perf-stats").json()["content_storage"]
    # Two GETs in between -> +2. The third stats-fetch doesn't bump.
    assert after["list_all_calls"] - before["list_all_calls"] == 2


def test_playlist_storage_load_all_counter_increments(client: TestClient):
    """load_all_calls -- the other major sweep target. Listing the
    playlists collection should route through load_all."""
    before = client.get("/api/system/perf-stats").json()["playlist_storage"]
    client.get("/api/playlists")
    after = client.get("/api/system/perf-stats").json()["playlist_storage"]
    assert after["load_all_calls"] >= before["load_all_calls"] + 1


def test_middleware_logs_each_request(client: TestClient):
    """Every HTTP request gets a request_log entry with the
    documented shape."""
    client.get("/healthz")
    client.get("/api/content")
    log = recent_requests()
    # At least our two requests landed; methods + paths preserved.
    paths = [e["path"] for e in log]
    assert "/healthz" in paths
    assert "/api/content" in paths
    # Required fields.
    for entry in log:
        assert {"method", "path", "status", "duration_ms", "request_id"} <= entry.keys()
        assert isinstance(entry["duration_ms"], float)
        assert entry["duration_ms"] >= 0


# --- Batch 16.3 / sweep #8 A4: correlation-id round-trip ---


def test_middleware_echoes_inbound_request_id(client: TestClient):
    """X-Request-ID header on the request round-trips back on the
    response so a caller's trace can join across the wire."""
    response = client.get("/healthz", headers={"X-Request-ID": "phone-trace-42"})
    assert response.headers.get("x-request-id") == "phone-trace-42"
    # And the perf ring records it.
    assert any(e.get("request_id") == "phone-trace-42" for e in recent_requests())


def test_middleware_mints_request_id_when_absent(client: TestClient):
    """No inbound X-Request-ID -> middleware mints a 12-char hex id."""
    response = client.get("/healthz")
    rid = response.headers.get("x-request-id")
    assert rid is not None
    # uuid4().hex[:12] is 12 lowercase hex chars.
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)


def test_middleware_rejects_malformed_request_id(client: TestClient):
    """Junk input (control chars, header-injection attempt, too long)
    is dropped in favor of a minted id rather than reflected back
    into logs and response headers."""
    # Header value with a CRLF injection attempt -- httpx strips the
    # CRLF at send so the actual value the middleware sees is the
    # safe leading slice. Cover the long-input + special-char paths
    # with values httpx will actually pass through.
    response = client.get(
        "/healthz",
        headers={"X-Request-ID": "a" * 100},  # over 64
    )
    rid = response.headers.get("x-request-id")
    assert rid is not None
    assert len(rid) == 12  # minted, not echoed

    response = client.get(
        "/healthz",
        headers={"X-Request-ID": "has spaces!"},  # not alnum/-
    )
    rid = response.headers.get("x-request-id")
    assert rid is not None
    assert len(rid) == 12  # minted, not echoed


def test_coerce_request_id_rejects_crlf_injection():
    """Header-injection attempt with embedded \\r\\n must NOT reflect
    back -- httpx strips CRLF at send-time so the TestClient-level
    tests don't reach the predicate. Hit it directly with raw bytes
    here so the no-reflection guarantee is locked in."""
    headers = [(b"x-request-id", b"abc\r\nInjected: header")]
    result = _coerce_request_id(headers)
    assert "\r" not in result
    assert "\n" not in result
    assert "Injected" not in result
    assert len(result) == 12  # minted, hex


def test_coerce_request_id_rejects_non_ascii():
    """Bytes that can't decode to ASCII (Latin-1 \\xff, multi-byte
    UTF-8 starts) get a minted id rather than crashing or being
    reflected back."""
    headers = [(b"x-request-id", b"\xff\xfe garbage")]
    result = _coerce_request_id(headers)
    assert len(result) == 12  # minted, hex


def test_coerce_request_id_rejects_oversized():
    """65+ char values exceed the predicate cap and get minted
    instead -- prevents log-line bloat / response-header bloat from
    a misbehaving client."""
    headers = [(b"x-request-id", b"a" * 100)]
    result = _coerce_request_id(headers)
    assert len(result) == 12
    assert result != "a" * 12  # truly minted, not truncated input


def test_coerce_request_id_rejects_null_bytes():
    """Null bytes are non-printable -- they shouldn't sneak past the
    alnum-or-dash predicate. Mint instead."""
    headers = [(b"x-request-id", b"abc\x00def")]
    result = _coerce_request_id(headers)
    assert "\x00" not in result
    assert len(result) == 12


def test_coerce_request_id_accepts_valid_dash_alnum():
    """Sanity: a legitimate `phone-trace-42` shape round-trips."""
    headers = [(b"x-request-id", b"phone-trace-42")]
    result = _coerce_request_id(headers)
    assert result == "phone-trace-42"


def test_request_id_log_filter_stamps_record():
    """The Filter pulls the current ContextVar value onto the
    LogRecord so a Formatter with %(request_id)s renders correctly."""
    import logging

    from openmarquee.perf_middleware import RequestIdLogFilter, request_id_var

    token = request_id_var.set("test-id-abc")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="",
            args=(),
            exc_info=None,
        )
        RequestIdLogFilter().filter(record)
        assert record.request_id == "test-id-abc"
    finally:
        request_id_var.reset(token)
    # Outside a request scope, default is "-".
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    )
    RequestIdLogFilter().filter(record)
    assert record.request_id == "-"


def test_middleware_request_log_is_bounded(client: TestClient):
    """The ring tops out at _REQUEST_LOG_MAX -- otherwise a
    long-running device with chatty /api/playback/state polls would
    leak memory."""
    # Fire one more than the cap.
    for _ in range(_REQUEST_LOG_MAX + 10):
        client.get("/healthz")
    log = recent_requests()
    assert len(log) == _REQUEST_LOG_MAX


def test_font_cache_info_surfaces_through_endpoint(client: TestClient):
    """Verify the text_raster font cache info shows up. Sweep #2's
    perf baseline cares whether the cache is actually being hit
    on hot auto-render paths."""
    from openmarquee.text_raster import load_font

    load_font("Inter", 32, 100)  # cold load -> miss
    load_font("Inter", 32, 100)  # warm -> hit
    body = client.get("/api/system/perf-stats").json()
    assert body["font_cache"]["hits"] >= 1
    assert body["font_cache"]["misses"] >= 1


# --- Batch 7.2: json_parses counter tests ---


def test_playlist_storage_cache_skips_json_parse(client: TestClient):
    """Two GET /api/playlists in a row -- load_all_calls bumps both
    times, but json_parses bumps only on the cold-cache first call.
    This is the cache working: repeated reads return the cached
    PlaylistCollection without re-parsing the json file."""
    client.get("/api/playlists")
    before = client.get("/api/system/perf-stats").json()["playlist_storage"]
    cold_parses = before["json_parses"]
    cold_load_alls = before["load_all_calls"]
    for _ in range(10):
        client.get("/api/playlists")
    after = client.get("/api/system/perf-stats").json()["playlist_storage"]
    # load_all_calls bumps every time (no cache short-circuit).
    assert after["load_all_calls"] - cold_load_alls == 10
    # json_parses doesn't move -- the cache hit returns without parse.
    assert after["json_parses"] == cold_parses


# --- 2026-05-24: Perf-outside-Auth (records 401s) ---


def test_perf_ring_records_auth_rejected_401s(monkeypatch, tmp_path):
    """Stack-order regression: PerfMiddleware must wrap AuthMiddleware
    so that 401-rejected requests still land in the in-memory perf
    ring. Pre-2026-05-24, Perf was innermost and Auth's short-circuit
    on un-authorized requests meant Perf never saw them — undermined
    the ring's value for high-loss / 401-flood diagnostics.

    This test re-enables auth (default conftest disables it via
    OPENMARQUEE_DISABLE_AUTH=1 to avoid mass token-minting in
    suites that don't care), points the storage at an empty tmp dir
    so the bearer-token gate fails closed, then verifies a request
    that gets 401-ed STILL appears in the recent_requests() ring."""
    # Re-enable auth for this test only.
    monkeypatch.delenv("OPENMARQUEE_DISABLE_AUTH", raising=False)
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    # Clear the auth-storage singleton's lru_cache so the new env var
    # is observed (same pattern as test_auth.py / test_csp_middleware).
    from openmarquee.dependencies import _auth_storage_singleton

    _auth_storage_singleton.cache_clear()
    try:
        with TestClient(app) as client:
            # /api/system/info requires a token; with no AuthState
            # configured, it 401s with "password not configured".
            resp = client.get("/api/system/info")
            assert resp.status_code == 401, "expected auth-gate 401"
            # Now the smoking gun: the 401'd request MUST appear in the
            # perf ring. Pre-fix this assertion would have failed
            # because Perf was inside Auth and never saw the request.
            log = recent_requests()
            paths_and_statuses = [(e["path"], e["status"]) for e in log]
            assert ("/api/system/info", 401) in paths_and_statuses, (
                "401-rejected request didn't reach the perf ring — "
                "PerfMiddleware is likely positioned INSIDE "
                "AuthMiddleware again. Check app.py add_middleware "
                "order; Perf must be outer (last-added-or-near-last) "
                "so Auth's short-circuit doesn't bypass it."
            )
    finally:
        _auth_storage_singleton.cache_clear()
