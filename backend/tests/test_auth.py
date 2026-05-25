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
    token, updated = mint_token(state)
    assert token.startswith("7.")
    # secret part is token_urlsafe(32) -> 43 b64 chars
    assert len(token.split(".", 1)[1]) == 43
    # New state carries the argon2 hash of the issued secret.
    assert updated.issued_token_hash is not None


@pytest.mark.asyncio
async def test_verify_token_returns_true_for_freshly_minted_token():
    """The fresh-mint path: mint_token returns (token, updated_state);
    verify_token against the updated state argon2-verifies + accepts."""
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token, updated = mint_token(state)
    assert await verify_token(token, updated) is True


@pytest.mark.asyncio
async def test_verify_token_returns_false_after_version_bump():
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token, updated = mint_token(state)
    state_bumped = updated.model_copy(update={"token_version": 4})
    assert await verify_token(token, state_bumped) is False


@pytest.mark.asyncio
async def test_verify_token_returns_false_for_malformed():
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"))
    assert await verify_token("", state) is False
    assert await verify_token("no-dot-no-version", state) is False
    assert (
        await verify_token(
            "notanumber.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            state,
        )
        is False
    )
    # Wrong-length secret part:
    assert await verify_token("1.short", state) is False


@pytest.mark.asyncio
async def test_verify_token_returns_false_when_state_is_none():
    """Auth not configured -- nothing to verify against."""
    token, _ = mint_token(AuthState(password_hash=hash_password("pw")))
    assert await verify_token(token, None) is False


# ---- 2026-05-24 security audit Path A: real secret verification ----


@pytest.mark.asyncio
async def test_verify_token_rejects_forged_secret_with_correct_version_prefix():
    """THE FIX (audit finding 1 HIGH). Pre-fix: any `<version>.<43-
    char-b64>` validated as long as the version matched. Post-fix:
    the secret must argon2-verify against state.issued_token_hash.
    """
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    real_token, updated = mint_token(state)
    real_version, _real_secret = real_token.split(".", 1)
    # Forge a token: same version prefix, 43-char b64-safe random secret
    # the attacker generated themselves.
    forged_secret = "Z" * 43  # well-formed shape, not the real secret
    forged_token = f"{real_version}.{forged_secret}"
    assert forged_token != real_token
    assert await verify_token(forged_token, updated) is False


@pytest.mark.asyncio
async def test_verify_token_rejects_unknown_version():
    """A token whose version prefix doesn't match state.token_version
    rejects without even touching argon2 (cheap fail)."""
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    _, updated = mint_token(state)
    # Reuse the valid hash's secret but lie about the version prefix.
    fake = f"99.{'a' * 43}"
    assert await verify_token(fake, updated) is False


@pytest.mark.asyncio
async def test_verify_token_rejects_when_no_hash_persisted_yet():
    """A state with token_version set but issued_token_hash=None (e.g.,
    fresh AuthState before the first mint, or a corrupt-state
    recovery) must NOT accept any token -- otherwise the rotation
    lever would be the only gate."""
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token = f"3.{'x' * 43}"
    assert state.issued_token_hash is None
    assert await verify_token(token, state) is False


@pytest.mark.asyncio
async def test_verify_token_rejects_noncanonical_version_prefix():
    """Round-10 security DiD: int() accepts '+1', '01', '001', '1_0',
    ' 1 ', '٠١' (Arabic-Indic), etc. -- all decode to the same int.
    Pre-fix, each non-canonical prefix populated its own cache entry
    after argon2-verifying. Attacker with a leaked valid token could
    sustain requests varying ONLY the version prefix and inflate
    _VERIFIED_TOKEN_CACHE unboundedly until OOM. mint_token emits
    canonical decimal str of an int via f-string, so legitimate tokens
    are never non-canonical.
    """
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    real_token, updated = mint_token(state)
    _, real_secret = real_token.split(".", 1)
    # Canonical form first: this MUST verify (sanity baseline that the
    # rejection isn't over-zealous).
    assert await verify_token(real_token, updated) is True
    # Each of these is a different STRING but int() of the version
    # prefix decodes to 3. Pre-fix all would have argon2-verified +
    # cached. Post-fix they all reject at the canonical-form check
    # before reaching argon2.
    noncanonical_forms = [
        f"+3.{real_secret}",  # leading + sign
        f"03.{real_secret}",  # leading zero (2-char)
        f"003.{real_secret}",  # leading zeros (3-char)
        f"0003.{real_secret}",
        f"1_3.{real_secret}",  # underscore separator (Python 3 int literal)
        f" 3.{real_secret}",  # leading whitespace
        f"3 .{real_secret}",  # trailing whitespace in version part
        f"\t3.{real_secret}",  # tab prefix
        f"٠٠٣.{real_secret}",  # Arabic-Indic 003 -- isdigit() True but
        # str(int(...)) != original
    ]
    for bad in noncanonical_forms:
        assert await verify_token(bad, updated) is False, (
            f"non-canonical version prefix must reject: {bad!r}"
        )


@pytest.mark.asyncio
async def test_verify_token_cache_capped_at_lru_limit():
    """Round-10 security DiD: cache is bounded by _VERIFIED_TOKEN_CACHE_CAP
    via OrderedDict LRU eviction. Even if an attacker found a NEW
    cache-key-mangling vector, the dict can't exceed the cap.

    Test: monkeypatch the cap to a small number, fill with distinct
    tokens (forcing distinct argon2-verify-then-insert paths), assert
    the dict stays at the cap AND the oldest entries are evicted.
    """
    from openmarquee import auth as auth_module
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    original_cap = auth_module._VERIFIED_TOKEN_CACHE_CAP
    auth_module._VERIFIED_TOKEN_CACHE_CAP = 4
    try:
        state = AuthState(password_hash=hash_password("pw"), token_version=1)
        # Mint once to get a real issued_token_hash; monkeypatch
        # _argon2_verify_secret to always-True so we can stuff distinct
        # tokens without 43 real argon2 verifies (~10s on Mac).
        _, updated = mint_token(state)

        def always_true(_h, _s):
            return True

        original_verify = auth_module._argon2_verify_secret
        auth_module._argon2_verify_secret = always_true
        try:
            # Insert cap + 3 distinct tokens. token_version is 1 for
            # all (real auth would reject the version mismatch since
            # there's only one valid secret, but our monkeypatched
            # verify is unconditional -- the test is about the cache
            # accounting, not the verify logic).
            tokens = [f"1.{chr(ord('a') + i)}{'b' * 42}" for i in range(7)]
            for t in tokens:
                assert await verify_token(t, updated) is True
            # Cap holds: never exceeds 4 regardless of input volume.
            assert len(auth_module._VERIFIED_TOKEN_CACHE) == 4
            # Oldest 3 evicted (LRU: insertion order = access order
            # since each was a fresh insert with no re-touch).
            for evicted in tokens[:3]:
                assert evicted not in auth_module._VERIFIED_TOKEN_CACHE, (
                    f"oldest entry should have been LRU-evicted: {evicted!r}"
                )
            # Most-recent 4 retained.
            for retained in tokens[3:]:
                assert retained in auth_module._VERIFIED_TOKEN_CACHE, (
                    f"recent entry should be cached: {retained!r}"
                )
        finally:
            auth_module._argon2_verify_secret = original_verify
    finally:
        auth_module._VERIFIED_TOKEN_CACHE_CAP = original_cap
        clear_verified_token_cache()


@pytest.mark.asyncio
async def test_verify_token_cache_hit_moves_entry_to_recent():
    """Round-10 security DiD: cache HIT must move_to_end so eviction
    targets the truly-stalest entry, not the most-touched. Without
    this LRU touch, a hot token would be evicted first when newer
    cold tokens stream in.
    """
    from openmarquee import auth as auth_module
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    original_cap = auth_module._VERIFIED_TOKEN_CACHE_CAP
    auth_module._VERIFIED_TOKEN_CACHE_CAP = 3
    try:
        state = AuthState(password_hash=hash_password("pw"), token_version=1)
        _, updated = mint_token(state)

        def always_true(_h, _s):
            return True

        original_verify = auth_module._argon2_verify_secret
        auth_module._argon2_verify_secret = always_true
        try:
            t1 = f"1.{'a' + 'b' * 42}"
            t2 = f"1.{'c' + 'b' * 42}"
            t3 = f"1.{'d' + 'b' * 42}"
            t4 = f"1.{'e' + 'b' * 42}"
            assert await verify_token(t1, updated) is True
            assert await verify_token(t2, updated) is True
            assert await verify_token(t3, updated) is True
            # Touch t1 (the original oldest) — move-to-end should
            # shift the cold-end from t1 to t2.
            assert await verify_token(t1, updated) is True
            # Now insert t4 at capacity: t2 (post-touch coldest) must
            # evict, NOT t1 (which is now the most-recent).
            assert await verify_token(t4, updated) is True
            assert t1 in auth_module._VERIFIED_TOKEN_CACHE, (
                "re-touched t1 must NOT be evicted (LRU bug if it is)"
            )
            assert t2 not in auth_module._VERIFIED_TOKEN_CACHE, (
                "coldest-after-touch t2 must be evicted"
            )
            assert t3 in auth_module._VERIFIED_TOKEN_CACHE
            assert t4 in auth_module._VERIFIED_TOKEN_CACHE
        finally:
            auth_module._argon2_verify_secret = original_verify
    finally:
        auth_module._VERIFIED_TOKEN_CACHE_CAP = original_cap
        clear_verified_token_cache()


@pytest.mark.asyncio
async def test_verify_token_cache_short_circuits_second_call(monkeypatch):
    """Second verify with the same token must skip argon2 (cache hit).
    Functional check via monkeypatching the _HASHER.verify call --
    expect exactly one call across two verifies."""
    from openmarquee import auth as auth_module
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token, updated = mint_token(state)
    call_count = {"n": 0}
    real_verify = auth_module._argon2_verify_secret

    def counting_verify(*args, **kwargs):
        call_count["n"] += 1
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(auth_module, "_argon2_verify_secret", counting_verify)
    assert await verify_token(token, updated) is True
    assert await verify_token(token, updated) is True
    assert call_count["n"] == 1, (
        f"expected 1 argon2.verify call across 2 verifies (cache hit on "
        f"second); got {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_change_password_clears_verified_token_cache():
    """change_password must flush the cache so a previously-verified-
    but-now-rotated token can't ride out the 10min TTL window."""
    from openmarquee.auth import _VERIFIED_TOKEN_CACHE, clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token, updated = mint_token(state)
    # Populate the cache.
    assert await verify_token(token, updated) is True
    assert token in _VERIFIED_TOKEN_CACHE
    # change_password is sync but calls clear_verified_token_cache.
    change_password(updated, "new-password")
    assert _VERIFIED_TOKEN_CACHE == {}, "change_password did not flush the verified-token cache"


@pytest.mark.asyncio
async def test_consecutive_mints_invalidate_the_older_token():
    """Operator-visible consequence of the {version: single_hash}
    design: minting a new token under the same version invalidates the
    previous one (logging in on a second device kicks the first off).
    This pins the trade-off so a future "multi-session" refactor
    surfaces it explicitly."""
    from openmarquee.auth import clear_verified_token_cache

    clear_verified_token_cache()
    state = AuthState(password_hash=hash_password("pw"), token_version=3)
    token_a, state_after_a = mint_token(state)
    assert await verify_token(token_a, state_after_a) is True
    # Second mint under the same version overwrites the issued hash.
    # mint_token MUST flush the verified-token cache internally --
    # otherwise token_a's cached-True entry would ride out the 10min
    # TTL even though the issued_token_hash has rotated. Don't add a
    # manual clear_verified_token_cache() here; that would mask the
    # bug the subagent caught.
    token_b, state_after_b = mint_token(state_after_a)
    assert state_after_b.token_version == state_after_a.token_version
    assert await verify_token(token_a, state_after_b) is False
    assert await verify_token(token_b, state_after_b) is True


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


@pytest.mark.asyncio
async def test_set_password_concurrent_race_serialized_first_writer_wins(
    tmp_path: Path,
):
    """Round-9 set-password serialization behavior pin.

    The asyncio.Lock added in api_auth.py wraps the load->check->save
    trio in set_password. This test verifies the OBSERVABLE behavior
    under concurrent POSTs: exactly one 200 + one 409, and the on-disk
    hash matches the WINNER's password (not silently overwritten by
    the loser's later-arriving save).

    Honest caveat on bug-fix-tests-the-bug: under the current
    single-worker uvicorn + fully-sync handler body, removing the
    asyncio.Lock does NOT make this test fail. Python's asyncio
    serializes handler bodies at completion granularity (no await
    point exists between auth.load() and auth.save() in the
    pre-fix code), so the two coroutines never interleave inside
    the critical section. The TOCTOU race becomes exploitable in
    two future scenarios this test protects against:
      1. Multi-worker uvicorn (separate processes; each has its own
         _SET_PASSWORD_LOCK -- the file-system layer would need an
         O_EXCL or fcntl.flock fix as documented in the Lock comment)
      2. A future code change introducing an `await` between the
         load() check and the save() commit (e.g., async hash_password
         or async auth.save) -- then the lock becomes load-bearing
         and removing it would make this test fail.

    Test serves as the regression fence for both scenarios + pins the
    current observable behavior so a future code change can't silently
    drift it.
    """
    import asyncio

    import httpx

    from openmarquee.app import app
    from openmarquee.dependencies import _auth_storage_singleton, get_auth_storage

    auth_path = tmp_path / "auth.json"
    _auth_storage_singleton.cache_clear()
    with patch.dict(os.environ, {"OPENMARQUEE_AUTH_PATH": str(auth_path)}):
        os.environ.pop("OPENMARQUEE_DISABLE_AUTH", None)
        # Build an AuthStorage bound to tmp_path and override the
        # dependency so both async requests see the same backing file.
        auth = AuthStorage(auth_path)
        app.dependency_overrides[get_auth_storage] = lambda: auth
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                payload_a = {
                    "password": "operator-password-A",
                    "password_confirm": "operator-password-A",
                }
                payload_b = {
                    "password": "attacker-password-B",
                    "password_confirm": "attacker-password-B",
                }
                results = await asyncio.gather(
                    client.post("/api/auth/set-password", json=payload_a),
                    client.post("/api/auth/set-password", json=payload_b),
                )
            # Exactly one 200 and one 409 -- never two 200s (would mean
            # the race wasn't serialized).
            status_codes = sorted(r.status_code for r in results)
            assert status_codes == [200, 409], (
                f"expected one 200 + one 409 (serialized), got {status_codes}"
            )
            # The on-disk hash must match the WINNER's password, never
            # the loser's. Pre-fix the loser's hash would overwrite the
            # winner's via last-write-wins rename.
            persisted = auth.load()
            assert persisted is not None, "winner should have persisted state"
            winner_password = (
                "operator-password-A" if results[0].status_code == 200 else "attacker-password-B"
            )
            loser_password = (
                "attacker-password-B"
                if winner_password == "operator-password-A"
                else "operator-password-A"
            )
            assert verify_password(persisted.password_hash, winner_password), (
                "persisted hash must verify the winner's password"
            )
            assert not verify_password(persisted.password_hash, loser_password), (
                "persisted hash must NOT verify the loser's password "
                "(would mean last-write-wins overwrite)"
            )
        finally:
            app.dependency_overrides.pop(get_auth_storage, None)
    _auth_storage_singleton.cache_clear()


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


# ---- 2026-05-25 Bundle B2 item 7: set-password loopback grace ----


def test_set_password_grace_helper_reports_active_at_module_import(monkeypatch):
    """The module-level _BOOT_MONOTONIC capture means the grace is
    active from process import. A bare assertion right after import
    confirms the helper returns True when called in the first 30s of
    the process's life -- which is always-true in pytest fast-loop
    runs."""
    from openmarquee import api_auth

    # In a long pytest run this could conceivably be False; force the
    # window open by resetting the boot time to "just now."
    monkeypatch.setattr(api_auth, "_BOOT_MONOTONIC", api_auth.time.monotonic())
    assert api_auth._set_password_grace_active() is True


def test_set_password_grace_helper_closes_after_window(monkeypatch):
    """After _SET_PASSWORD_GRACE_SECONDS the helper returns False.
    Monkeypatch _BOOT_MONOTONIC to be 31s ago so the 30s window has
    elapsed without needing to actually sleep."""
    from openmarquee import api_auth

    monkeypatch.setattr(
        api_auth,
        "_BOOT_MONOTONIC",
        api_auth.time.monotonic() - api_auth._SET_PASSWORD_GRACE_SECONDS - 1.0,
    )
    assert api_auth._set_password_grace_active() is False


def test_set_password_during_grace_rejects_public_ip(monkeypatch, tmp_path):
    """During grace, a request from a public IP must 403 BEFORE the
    handler does anything. TestClient sends from `testclient` /
    `127.0.0.1` by default -- monkeypatch the IP check to return
    False so we simulate a public-IP source.

    The realistic threat scenario this closes: a fresh-boot device
    on the operator's LAN, where a malicious LAN device races the
    operator to POST set-password. The grace narrows the attack
    surface to trusted-network sources only during the boot window."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    # Force grace active.
    from openmarquee import api_auth

    monkeypatch.setattr(api_auth, "_BOOT_MONOTONIC", api_auth.time.monotonic())
    # Force the IP check to return False (simulating a public IP).
    monkeypatch.setattr(api_auth, "_is_private_or_loopback_ip", lambda host: False)

    from openmarquee.app import app

    client = TestClient(app)
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    assert response.status_code == 403, (
        f"expected 403 during grace from public IP; got {response.status_code}"
    )
    body = response.json()
    assert "trusted-network" in body.get("detail", "").lower()


def test_set_password_during_grace_accepts_loopback(monkeypatch, tmp_path):
    """During grace, a request from loopback / RFC1918 / Tailscale
    CGNAT must pass through to the handler. TestClient's default
    client IP is `testclient` which gets parsed via _is_private_or_
    loopback_ip; force the helper to return True to simulate the
    intended trusted-network source."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    from openmarquee.dependencies import _auth_storage_singleton

    _auth_storage_singleton.cache_clear()
    from openmarquee import api_auth

    monkeypatch.setattr(api_auth, "_BOOT_MONOTONIC", api_auth.time.monotonic())
    monkeypatch.setattr(api_auth, "_is_private_or_loopback_ip", lambda host: True)

    from openmarquee.app import app

    client = TestClient(app)
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    assert response.status_code == 200, (
        f"expected 200 during grace from trusted IP; got {response.status_code}"
    )


def test_set_password_after_grace_accepts_public_ip(monkeypatch, tmp_path):
    """After grace expires, the endpoint reverts to its pre-fix
    posture (reachable from any IP). Otherwise an operator who
    doesn't set the password within the 30s window would be locked
    out forever from non-loopback access."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    from openmarquee.dependencies import _auth_storage_singleton

    _auth_storage_singleton.cache_clear()
    from openmarquee import api_auth

    # Force grace EXPIRED.
    monkeypatch.setattr(
        api_auth,
        "_BOOT_MONOTONIC",
        api_auth.time.monotonic() - api_auth._SET_PASSWORD_GRACE_SECONDS - 1.0,
    )
    # IP check would return False (public IP), but since grace is
    # expired the check shouldn't even run.
    monkeypatch.setattr(api_auth, "_is_private_or_loopback_ip", lambda host: False)

    from openmarquee.app import app

    client = TestClient(app)
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    assert response.status_code == 200, (
        f"expected 200 after grace from any IP; got {response.status_code}"
    )


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
        assert response.status_code != 401, f"middleware 401'd a whitelisted path: {path}"


def test_test_harness_paths_whitelisted(client: TestClient):
    """The Live-takeover fake-camera harness at ui/test/ must be
    reachable without a bearer token, same auth shape as /dist/.

    The HTML page loads BEFORE the inline JS can read the operator's
    localStorage token; gating the page itself would 401 the harness
    on every load. The JS that runs after the page loads still attaches
    the bearer header to its /api/live/* calls.

    (Same StaticFiles caveat as test_ui_html_pages_whitelisted above:
    the test backend doesn't actually mount ui/, so 404 is fine —
    only 401 from the auth middleware fails this test.)
    """
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    for path in (
        "/test/fake-camera.html",
        "/test/fixture.mp4",
        "/test/README.md",
    ):
        response = client.get(path)
        assert response.status_code != 401, f"middleware 401'd a whitelisted /test/ path: {path}"


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
    response = client.get("/api/content", headers={"Authorization": "Bearer 1.invalidtoken"})
    assert response.status_code == 401


def test_protected_path_succeeds_with_valid_token(client: TestClient):
    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    response = client.get("/api/content", headers={"Authorization": f"Bearer {token}"})
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
        f"middleware 401'd a valid token-query for media route (got {response.status_code})"
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


# ---- 2026-05-24 security audit MEDIUM finding 3: response-header
# ---- hardening when query-string token auth is used. Mitigates the
# ---- URL-leak surface (browser history / server logs / Referer
# ---- header) the query-param fallback inherently has.


def test_query_auth_response_includes_no_store_and_no_referrer(client: TestClient):
    """A request authed via ?token=... must come back with both
    Cache-Control: no-store AND Referrer-Policy: no-referrer so the
    URL+body can't be cached locally / by intermediaries and the
    token can't leak via outgoing-link Referer."""
    import uuid

    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    bogus = uuid.uuid4()
    response = client.get(f"/api/content/{bogus}/asset?token={token}")
    # The endpoint 404s (no asset seeded), but the response headers
    # ARE the load-bearing assertion -- the wrap fires on the
    # response-start message regardless of status code, since the
    # middleware ran before handler dispatch and the wrap is on
    # `send`, not conditional on success.
    assert response.headers.get("cache-control") == "no-store", (
        f"expected Cache-Control: no-store; got {response.headers.get('cache-control')!r}"
    )
    assert response.headers.get("referrer-policy") == "no-referrer", (
        f"expected Referrer-Policy: no-referrer; got {response.headers.get('referrer-policy')!r}"
    )


def test_header_auth_response_does_not_include_query_hardening(client: TestClient):
    """A request authed via Authorization header (no query-string
    fallback) does NOT get the query-auth hardening headers -- there's
    no URL-leak surface to mitigate, so adding the headers would be
    no-value noise that disables caching for the common case."""
    import uuid

    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    bogus = uuid.uuid4()
    response = client.get(
        f"/api/content/{bogus}/asset",
        headers={"Authorization": f"Bearer {token}"},
    )
    # The query-auth wrap should NOT have fired (no token in URL).
    # Cache-Control should NOT be the wrap's `no-store` value.
    # Referrer-Policy IS now set globally by the CSP middleware's
    # Bundle A item 8 defense-in-depth headers (2026-05-25) -- but
    # to the GLOBAL default `strict-origin-when-cross-origin`, NOT
    # the stricter `no-referrer` the auth wrap would have set.
    assert response.headers.get("cache-control") != "no-store", (
        "header-bearer request unexpectedly carries Cache-Control: no-store "
        "(the auth wrap fired without query-auth)"
    )
    assert response.headers.get("referrer-policy") != "no-referrer", (
        "header-bearer request unexpectedly carries Referrer-Policy: no-referrer "
        "(the auth wrap fired without query-auth; should be the CSP global default)"
    )
    # Belt-and-suspenders: confirm the GLOBAL default IS the one the
    # CSP middleware set (Bundle A item 8 regression-lock).
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_header_wins_query_token_present_response_has_no_query_hardening(
    client: TestClient,
):
    """When BOTH Authorization header AND ?token= are present, the
    header wins (existing test_authorization_header_wins_when_both_
    supplied) and the response should NOT carry the query-auth
    hardening -- the request didn't actually use query-string auth."""
    import uuid

    set_resp = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    token = set_resp.json()["token"]
    bogus = uuid.uuid4()
    response = client.get(
        f"/api/content/{bogus}/asset?token=garbage",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code != 401
    assert "cache-control" not in {k.lower() for k in response.headers}, (
        "header-bearer-wins request shouldn't get query-auth hardening"
    )


def test_failed_query_auth_does_not_add_hardening_to_401(client: TestClient):
    """A request with a bad ?token= returns 401 via _send_401 BEFORE
    the wrap is installed (the wrap is conditional on verify_token
    succeeding). The 401 response stays clean."""
    import uuid

    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    bogus = uuid.uuid4()
    response = client.get(f"/api/content/{bogus}/asset?token=garbage")
    assert response.status_code == 401
    # 401 path runs _send_401 directly, not through the wrap.
    assert response.headers.get("cache-control") != "no-store", (
        "failed-auth 401 shouldn't carry query-auth hardening "
        "(the wrap installs AFTER verify_token succeeds)"
    )
