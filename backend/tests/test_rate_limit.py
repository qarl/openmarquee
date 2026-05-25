"""Tests for the token-bucket rate-limit helper.

Bundle B2 item 7. Covers the pure-Python TokenBucket primitive +
the client_ip_or_unknown safe-default + the wired-into-/api/auth/
login behavior including XFF trust semantics.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openmarquee._rate_limit import (
    LOGIN_BUCKET,
    UNKNOWN_IP_KEY,
    TokenBucket,
    client_ip_or_unknown,
)

# ---- TokenBucket primitive ----


def test_token_bucket_allows_under_capacity():
    """The first N attempts (up to capacity) all pass."""
    bucket = TokenBucket(capacity=5, window_seconds=60.0)
    for _ in range(5):
        assert bucket.try_take("ip-A") is True


def test_token_bucket_blocks_over_capacity():
    """The N+1th attempt within the window returns False."""
    bucket = TokenBucket(capacity=5, window_seconds=60.0)
    for _ in range(5):
        bucket.try_take("ip-A")
    assert bucket.try_take("ip-A") is False


def test_token_bucket_isolates_distinct_keys():
    """Per-key buckets: exhausting one key's tokens leaves another
    key's untouched. This is the LOAD-BEARING invariant -- if all
    traffic shared one bucket, a single attacker would lock out
    every legitimate operator."""
    bucket = TokenBucket(capacity=3, window_seconds=60.0)
    for _ in range(3):
        bucket.try_take("ip-A")
    # ip-A is exhausted.
    assert bucket.try_take("ip-A") is False
    # ip-B starts fresh with full capacity.
    for _ in range(3):
        assert bucket.try_take("ip-B") is True


def test_token_bucket_refills_after_window(monkeypatch):
    """After window_seconds have passed since the OLDEST timestamp,
    that token frees up and try_take succeeds again."""
    import openmarquee._rate_limit as rl

    fake_time = [1000.0]

    def fake_monotonic():
        return fake_time[0]

    monkeypatch.setattr(rl.time, "monotonic", fake_monotonic)
    bucket = TokenBucket(capacity=3, window_seconds=10.0)
    for _ in range(3):
        bucket.try_take("ip-A")
    assert bucket.try_take("ip-A") is False
    # Advance past the window.
    fake_time[0] = 1020.0
    assert bucket.try_take("ip-A") is True


def test_token_bucket_failed_attempts_do_not_extend_window(monkeypatch):
    """When the bucket is exhausted, additional try_take calls must
    NOT add new timestamps -- otherwise hostile traffic could keep
    the window rolling forward and lock out legitimate retries
    forever (sliding-window-lock attack)."""
    import openmarquee._rate_limit as rl

    fake_time = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: fake_time[0])
    bucket = TokenBucket(capacity=2, window_seconds=10.0)
    bucket.try_take("ip-A")
    bucket.try_take("ip-A")
    # Now exhausted. Hostile traffic pounds the endpoint:
    fake_time[0] = 1005.0
    for _ in range(20):
        assert bucket.try_take("ip-A") is False
    # The original 2 timestamps were at t=1000 + t=1000; window
    # ends at t=1010. Once we cross that, the bucket frees up.
    fake_time[0] = 1011.0
    assert bucket.try_take("ip-A") is True


def test_token_bucket_reset_drops_all_state():
    """reset() with no key drops every bucket; with a specific key
    drops just that one. Used by tests to isolate cases; production
    use would be a future operator-rescue affordance."""
    bucket = TokenBucket(capacity=3, window_seconds=60.0)
    for _ in range(3):
        bucket.try_take("ip-A")
        bucket.try_take("ip-B")
    bucket.reset("ip-A")
    assert bucket.try_take("ip-A") is True
    # ip-B was untouched.
    assert bucket.try_take("ip-B") is False
    # Now reset everything.
    bucket.reset()
    assert bucket.try_take("ip-B") is True


# ---- client_ip_or_unknown safe-default ----


def test_client_ip_or_unknown_passes_through_real_ip():
    assert client_ip_or_unknown("192.168.1.50") == "192.168.1.50"


def test_client_ip_or_unknown_falls_back_for_none():
    assert client_ip_or_unknown(None) == UNKNOWN_IP_KEY


def test_client_ip_or_unknown_falls_back_for_empty():
    assert client_ip_or_unknown("") == UNKNOWN_IP_KEY
    assert client_ip_or_unknown("   ") == UNKNOWN_IP_KEY


def test_client_ip_or_unknown_falls_back_for_non_string():
    # request.client.host should be a string, but defensive coverage:
    assert client_ip_or_unknown(123) == UNKNOWN_IP_KEY  # type: ignore[arg-type]


# ---- /api/auth/login throttle integration ----


@pytest.fixture(autouse=True)
def _reset_login_bucket():
    """Each test starts with a clean LOGIN_BUCKET so the 5-attempt
    cap doesn't leak across cases."""
    LOGIN_BUCKET.reset()
    yield
    LOGIN_BUCKET.reset()


def _client_with_configured_auth(tmp_path, monkeypatch):
    """Returns a TestClient whose AuthStorage has a configured
    password ("hunter2hunter") so /api/auth/login is reachable
    (not 404'd as "not yet configured")."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    # Clear the lru_cache so the next get_auth_storage() resolves
    # against the new tmp_path. Without this, a previous test's
    # cached singleton points at a stale dir + the set-password POST
    # below writes to the wrong place, then login 404s as "not
    # configured" because the cached singleton's file doesn't exist.
    from openmarquee.dependencies import _auth_storage_singleton

    _auth_storage_singleton.cache_clear()
    # Bundle B2 item 7 set-password grace would 403 the TestClient
    # request below (TestClient sends from "testclient" which
    # ipaddress.ip_address rejects -> _is_private_or_loopback_ip
    # returns False -> grace gate rejects). Force-expire so
    # set-password sets up the test's preconditions.
    import time as _time

    from openmarquee import api_auth

    monkeypatch.setattr(
        api_auth,
        "_BOOT_MONOTONIC",
        _time.monotonic() - api_auth._SET_PASSWORD_GRACE_SECONDS - 1.0,
    )
    from openmarquee.app import app

    client = TestClient(app)
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    assert response.status_code == 200, (
        f"set-password setup failed: {response.status_code} {response.text}"
    )
    return client


def test_login_allows_five_attempts_then_429s_the_sixth(tmp_path, monkeypatch):
    """5 wrong-password POSTs all 401, 6th gets 429 from the throttle
    BEFORE the argon2 verify runs. Pins the 5-attempt budget."""
    client = _client_with_configured_auth(tmp_path, monkeypatch)
    for _ in range(5):
        resp = client.post("/api/auth/login", json={"password": "wrong-password"})
        assert resp.status_code == 401
    resp = client.post("/api/auth/login", json={"password": "wrong-password"})
    assert resp.status_code == 429, f"expected 429 throttle on 6th attempt; got {resp.status_code}"
    body = resp.json()
    assert "too many" in body.get("detail", "").lower(), (
        f"429 detail should mention throttle; got {body}"
    )


def test_login_ignores_xff_header_at_python_layer(tmp_path, monkeypatch):
    """Mandatory check #6 from the B2 dispatch (XFF spoof prevention).

    The trust boundary is enforced in TWO layers:
      1. uvicorn `--forwarded-allow-ips 127.0.0.1` (pinned in
         test_systemd_unit_passes_proxy_headers_to_uvicorn) -- the
         transport-layer gate; XFF from non-loopback peers is
         stripped before `request.client.host` is populated.
      2. The Python rate-limit code reads ONLY `request.client.host`,
         NEVER the raw X-Forwarded-For header -- so even if layer (1)
         is mis-configured or bypassed, an attacker can't forge a
         fresh bucket per attempt by varying XFF.

    This test pins layer (2). 5 wrong-password POSTs each with a
    DIFFERENT XFF header all share the same bucket (keyed off
    TestClient's peer "testclient"), so the 6th 429s. If a future
    refactor reaches for `request.headers.get("X-Forwarded-For")`
    directly, this test goes red.
    """
    client = _client_with_configured_auth(tmp_path, monkeypatch)
    forged_xff = [
        "1.2.3.4",
        "5.6.7.8",
        "9.10.11.12",
        "13.14.15.16",
        "17.18.19.20",
    ]
    for xff in forged_xff:
        resp = client.post(
            "/api/auth/login",
            json={"password": "wrong"},
            headers={"X-Forwarded-For": xff},
        )
        assert resp.status_code == 401, (
            f"setup: each pre-throttle attempt should 401, got {resp.status_code}"
        )
    # 6th attempt with yet-another fresh XFF: still 429 because the
    # bucket keys off `request.client.host` (= "testclient" for all 6),
    # not the forged XFF header.
    resp = client.post(
        "/api/auth/login",
        json={"password": "wrong"},
        headers={"X-Forwarded-For": "99.99.99.99"},
    )
    assert resp.status_code == 429, (
        f"XFF-spoof bypass leak: 6th attempt with a fresh XFF returned "
        f"{resp.status_code}; should be 429 (bucket must key off "
        f"request.client.host, not XFF)"
    )


def test_login_throttle_does_not_burn_argon2_on_throttled_request(tmp_path, monkeypatch):
    """The throttle gate must fire BEFORE verify_password so a hostile
    client can't burn ~250ms server CPU per blocked attempt. Functional
    check: monkeypatch verify_password to count calls; after 5 attempts
    + 1 throttled, verify_password was called exactly 5 times (not 6)."""
    client = _client_with_configured_auth(tmp_path, monkeypatch)
    from openmarquee import api_auth

    call_count = {"n": 0}
    real_verify = api_auth.verify_password

    def counting_verify(*args, **kwargs):
        call_count["n"] += 1
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(api_auth, "verify_password", counting_verify)
    for _ in range(6):
        client.post("/api/auth/login", json={"password": "wrong"})
    assert call_count["n"] == 5, (
        f"expected exactly 5 argon2 verifies (1 per pre-throttle attempt); "
        f"got {call_count['n']} -- throttle is firing AFTER verify, not BEFORE"
    )
