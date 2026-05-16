"""Tests for the Batch 20.1 backend auth foundation.

Covers:
  - hash_password / verify_password primitives
  - AuthStorage round-trip + 0600 mode + corruption recovery
  - mint_token / verify_token + token_version invalidation
  - api_auth endpoints (status, set-password, login, change-password)
  - AuthMiddleware whitelist + bearer-token gate
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from openmarquee.auth import (
    AuthState,
    AuthStorage,
    change_password,
    hash_password,
    mint_token,
    verify_password,
    verify_token,
)


# --- hash/verify primitives ---


def test_hash_password_returns_argon2id_format():
    h = hash_password("hunter2hunter")
    # PHC format: $argon2id$v=19$m=...$<salt>$<hash>
    assert h.startswith("$argon2id$")


def test_verify_password_correct():
    h = hash_password("hunter2hunter")
    assert verify_password(h, "hunter2hunter") is True


def test_verify_password_wrong_returns_false():
    h = hash_password("hunter2hunter")
    assert verify_password(h, "wrong-password") is False


def test_verify_password_returns_false_for_corrupt_hash():
    """A malformed PHC string in storage (operator hand-edit gone
    wrong) returns False rather than crashing the API layer. The
    AuthStorage corruption-recovery path catches the JSON case;
    this guards the "JSON parsed but hash field is garbage" branch."""
    assert verify_password("not-an-argon2-string", "anything") is False


# --- AuthStorage round-trip + persistence ---


def test_load_returns_none_when_not_configured(tmp_path: Path):
    storage = AuthStorage(tmp_path / "auth.json")
    assert storage.load() is None


def test_save_then_load_round_trips(tmp_path: Path):
    storage = AuthStorage(tmp_path / "auth.json")
    state = AuthState(password_hash=hash_password("hunter2hunter"))
    storage.save(state)
    loaded = storage.load()
    assert loaded is not None
    assert loaded.password_hash == state.password_hash
    assert loaded.token_version == 1


def test_save_persists_with_0600_mode(tmp_path: Path):
    storage = AuthStorage(tmp_path / "auth.json")
    storage.save(AuthState(password_hash=hash_password("hunter2hunter")))
    mode = storage.path.stat().st_mode & 0o777
    assert mode == 0o600


def test_corrupt_auth_json_recovers_to_not_configured(tmp_path: Path):
    path = tmp_path / "auth.json"
    path.write_text("{ not valid json ]")
    storage = AuthStorage(path)
    assert storage.load() is None
    # Quarantine sibling exists.
    corrupt = list(tmp_path.glob("auth.json.corrupt-*"))
    assert len(corrupt) == 1


# --- token mint / verify ---


def test_mint_token_includes_version_prefix():
    state = AuthState(password_hash=hash_password("pw"), token_version=7)
    token = mint_token(state)
    assert token.startswith("7.")
    # secret part is token_urlsafe(32) -> 43 b64 chars
    assert len(token.split(".", 1)[1]) == 43


def test_verify_token_returns_true_for_matching_version():
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token = mint_token(state)
    assert verify_token(token, state) is True


def test_verify_token_returns_false_after_version_bump():
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token = mint_token(state)
    state_bumped = state.model_copy(update={"token_version": 4})
    assert verify_token(token, state_bumped) is False


def test_verify_token_returns_false_for_malformed():
    state = AuthState(password_hash=hash_password("pw"))
    assert verify_token("", state) is False
    assert verify_token("no-dot-no-version", state) is False
    assert verify_token("notanumber.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", state) is False
    # Wrong-length secret part:
    assert verify_token("1.short", state) is False


def test_verify_token_returns_false_when_state_is_none():
    """Auth not configured -- nothing to verify against."""
    token = mint_token(AuthState(password_hash=hash_password("pw")))
    assert verify_token(token, None) is False


def test_change_password_bumps_token_version():
    state = AuthState(password_hash=hash_password("old"), token_version=1)
    new_state = change_password(state, "new-password")
    assert new_state.token_version == 2
    assert verify_password(new_state.password_hash, "new-password") is True
    assert verify_password(new_state.password_hash, "old") is False


# --- api_auth endpoints (TestClient against the FastAPI app) ---


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """TestClient with an isolated AuthStorage path. Each test gets
    a fresh tmp_path so state doesn't leak across tests.

    OPENMARQUEE_DISABLE_AUTH is explicitly UNSET here (conftest sets
    it for every other suite) so the middleware actually gates --
    these tests exercise the auth flow itself, so they need the
    gate engaged."""
    from openmarquee.dependencies import _auth_storage_singleton

    # Point AuthStorage at the tmp_path; clear the lru_cache so the
    # next get_auth_storage() picks up the new path.
    auth_path = tmp_path / "auth.json"
    _auth_storage_singleton.cache_clear()
    # Remove DISABLE_AUTH so the middleware gates. The patch.dict
    # restores it on fixture teardown for the rest of the suite.
    with patch.dict(
        os.environ,
        {"OPENMARQUEE_AUTH_PATH": str(auth_path)},
    ):
        os.environ.pop("OPENMARQUEE_DISABLE_AUTH", None)
        try:
            from openmarquee.app import app

            with TestClient(app) as c:
                yield c
        finally:
            os.environ["OPENMARQUEE_DISABLE_AUTH"] = "1"
    _auth_storage_singleton.cache_clear()


def test_status_unconfigured_returns_false(client: TestClient):
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"configured": False}


def test_status_configured_returns_true(client: TestClient):
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    response = client.get("/api/auth/status")
    assert response.json() == {"configured": True}


def test_set_password_succeeds_first_time(client: TestClient):
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token"].startswith("1.")


def test_set_password_fails_when_already_configured(client: TestClient):
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    response = client.post(
        "/api/auth/set-password",
        json={"password": "another-pw", "password_confirm": "another-pw"},
    )
    assert response.status_code == 409


def test_set_password_fails_when_confirm_mismatch(client: TestClient):
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "different"},
    )
    assert response.status_code == 422


def test_set_password_fails_when_too_short(client: TestClient):
    response = client.post(
        "/api/auth/set-password",
        json={"password": "short", "password_confirm": "short"},
    )
    # min_length on the Pydantic field -> 422
    assert response.status_code == 422


def test_login_succeeds_with_correct_password(client: TestClient):
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    response = client.post("/api/auth/login", json={"password": "hunter2hunter"})
    assert response.status_code == 200
    assert response.json()["token"].startswith("1.")


def test_login_fails_with_wrong_password(client: TestClient):
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    response = client.post("/api/auth/login", json={"password": "wrong"})
    assert response.status_code == 401


def test_login_fails_when_not_configured(client: TestClient):
    response = client.post("/api/auth/login", json={"password": "anything-ok"})
    assert response.status_code == 404


def test_change_password_requires_auth(client: TestClient):
    """No bearer token at all -- middleware rejects with 401."""
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    response = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "hunter2hunter",
            "new_password": "newpass-1",
            "new_password_confirm": "newpass-1",
        },
    )
    assert response.status_code == 401


def test_change_password_requires_current_password(client: TestClient):
    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    response = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "wrong",
            "new_password": "newpass-1",
            "new_password_confirm": "newpass-1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_change_password_bumps_token_version_invalidating_old(client: TestClient):
    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    old_token = set_resp.json()["token"]
    new_resp = client.post(
        "/api/auth/change-password",
        json={
            "current_password": "hunter2hunter",
            "new_password": "newpass-1",
            "new_password_confirm": "newpass-1",
        },
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert new_resp.status_code == 200
    new_token = new_resp.json()["token"]
    assert new_token.startswith("2.")
    # The old token now fails verification on any protected route.
    response = client.get("/api/content", headers={"Authorization": f"Bearer {old_token}"})
    assert response.status_code == 401
    # The new token works.
    response = client.get("/api/content", headers={"Authorization": f"Bearer {new_token}"})
    assert response.status_code == 200


# --- middleware whitelist + gate ---


def test_whitelisted_paths_no_auth_required(client: TestClient):
    """A few whitelist entries should respond (200 or feature-specific
    code) without any Authorization header even before set-password."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_ui_html_pages_whitelisted(client: TestClient):
    """20.2: the captive-portal HTML pages must be reachable without
    a token. main.js redirects to /login.html / /set-password.html
    before any token exists -- if the middleware gated those URLs the
    operator would 401 on the page itself."""
    # Pre-configure auth so we're testing the "with auth state" branch
    # of the middleware (i.e. the whitelist actually has to short-
    # circuit; without it the middleware would 401 these as
    # "authentication required" since no Bearer is sent).
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    # The TestClient doesn't actually mount the StaticFiles UI dir
    # (UI root path isn't there in the test backend), so the response
    # may 404. The load-bearing assertion is that the middleware does
    # NOT 401 -- whether the static handler 404s is fine.
    for path in (
        "/login.html",
        "/set-password.html",
        "/welcome.html",
        "/index.html",
        "/dist/main.js",
        "/dist/login.js",
        "/dist/set-password.js",
        "/styles.css",
    ):
        response = client.get(path)
        assert response.status_code != 401, (
            f"middleware 401'd a whitelisted path: {path}"
        )


def test_protected_path_returns_401_without_token(client: TestClient):
    """Auth not configured + no token + non-whitelist path -> 401."""
    response = client.get("/api/content")
    assert response.status_code == 401
    assert "configured" in response.json()["detail"]


def test_protected_path_returns_401_with_invalid_token(client: TestClient):
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    response = client.get(
        "/api/content", headers={"Authorization": "Bearer 1.invalidtoken"}
    )
    assert response.status_code == 401


def test_protected_path_succeeds_with_valid_token(client: TestClient):
    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    response = client.get(
        "/api/content", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_flock_peer_endpoints_whitelisted(client: TestClient):
    """Peer-callable flock endpoints authenticate via Tailscale ACL
    not bearer; the middleware must whitelist them."""
    # /api/flock/manifest is GET and reads content storage; no auth
    # header is needed for the request to NOT 401 (it may 200 with
    # empty manifest or otherwise; the load-bearing claim is "no
    # 401 from middleware").
    response = client.get("/api/flock/manifest")
    assert response.status_code != 401


# Bug 3+4 (qarl 2026-05-16) — `?token=...` query-param fallback for
# media routes that bare <img>/<video> tags must reach. Authorization
# header still wins when present; query-param is the additive escape
# hatch for binary-blob endpoints only.


def test_media_route_accepts_token_query_param(client: TestClient):
    """`?token=<bearer>` works on /api/content/{id}/asset for an <img>
    src that can't carry an Authorization header."""
    import uuid

    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    # No content seeded in the test fixture, so the endpoint will 404 for
    # an unknown UUID -- the load-bearing assertion is "not 401".
    bogus = uuid.uuid4()
    response = client.get(f"/api/content/{bogus}/asset?token={token}")
    assert response.status_code != 401, (
        f"middleware 401'd a valid token-query for media route "
        f"(got {response.status_code})"
    )
    # Confirm a 404 (or other non-auth status) actually fires.
    assert response.status_code in (404, 200)


def test_media_route_query_token_with_bad_token_still_401(client: TestClient):
    import uuid

    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    bogus = uuid.uuid4()
    response = client.get(f"/api/content/{bogus}/asset?token=garbage")
    assert response.status_code == 401


def test_media_route_query_token_missing_still_401(client: TestClient):
    import uuid

    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    bogus = uuid.uuid4()
    response = client.get(f"/api/content/{bogus}/asset")
    assert response.status_code == 401


def test_non_media_route_rejects_token_query_param(client: TestClient):
    """`?token=` works ONLY for the media-route suffixes (/asset, /video).
    A protected metadata/list route stays bearer-only."""
    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    # /api/content (list) is gated; query-param should NOT auth here.
    response = client.get(f"/api/content?token={token}")
    assert response.status_code == 401


def test_playback_thumbnail_endpoints_whitelisted(client: TestClient):
    """Bug 5 (qarl 2026-05-16): the flock panel renders peer tiles by
    cross-origin GET against /api/playback/current-thumbnail and
    /current-frame. Our local bearer token can't authenticate against
    a peer's AuthState, so these endpoints rely on the CORS allowlist
    + tailnet ACL instead of header-bearer auth. Middleware MUST
    whitelist them; otherwise every peer-tile fetch 401s.
    """
    # Configure auth so we're testing the "with auth state" branch
    # of the middleware (without a configured AuthState a "not
    # configured" 401 would mask the whitelist test).
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    for path in (
        "/api/playback/current-thumbnail",
        "/api/playback/current-frame",
    ):
        response = client.get(path)
        # Whitelist enforced means status is NOT 401. The route handler
        # may still 204 / 404 / 503 (no asset, nothing playing) — the
        # load-bearing claim is "middleware doesn't reject this."
        assert response.status_code != 401, (
            f"middleware 401'd a whitelisted peer-callable path: {path}"
        )


def test_authorization_header_wins_when_both_supplied(client: TestClient):
    """If both Authorization header and ?token= are present, the header
    is consulted first and the query-param is ignored. Verifies the
    ordering at AuthMiddleware:159-164."""
    import uuid

    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    bogus = uuid.uuid4()
    # Valid header + garbage query: should pass (header wins).
    response = client.get(
        f"/api/content/{bogus}/asset?token=garbage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code != 401
