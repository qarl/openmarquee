"""REST API for password-based auth (Batch 20.1 / phase A.1).

Endpoints (all under `/api/auth`):

  GET  /api/auth/status         -- {configured: bool}; UNAUTH
  POST /api/auth/set-password   -- {password, password_confirm} ->
                                   {token}; first-time only (409 once
                                   configured); UNAUTH
  POST /api/auth/login          -- {password} -> {token}; UNAUTH
  POST /api/auth/change-password -- {current_password, new_password,
                                    new_password_confirm} -> {token};
                                    requires Authorization bearer;
                                    bumps token_version so old tokens
                                    stop verifying

See `auth.py` for the AuthState / hashing / token primitives. The
whitelist that lets these endpoints reach the user without a prior
bearer token lives in `auth_middleware.py`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from openmarquee._rate_limit import LOGIN_BUCKET, client_ip_or_unknown
from openmarquee.auth import (
    MIN_PASSWORD_LEN,
    AuthStorage,
    change_password,
    hash_password,
    mint_token,
    verify_password,
    verify_token,
)
from openmarquee.dependencies import get_auth_storage
from openmarquee.fqdn_redirect_middleware import _is_private_or_loopback_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 2026-05-25 Bundle B2 item 7: set-password loopback-grace boot timer.
#
# Captured at module-import time (i.e., process boot). For the first
# _SET_PASSWORD_GRACE_SECONDS of the process's life, /api/auth/set-
# password is reachable ONLY from loopback / RFC1918 / Tailscale CGNAT
# source IPs (trusted-peer-shaped). After the grace expires, the
# endpoint reverts to its prior behavior (reachable from any IP,
# returns 409 once configured).
#
# Threat closed: a fresh-boot device with no password is vulnerable
# to a LAN-attacker race -- whoever POSTs set-password first sets
# the password. The grace narrows the attack window to "trusted-
# network sources only" during the period when the operator is
# physically present and just-now booted the device. After grace
# expires, if the operator hasn't set a password, the device is
# back to the pre-fix posture; but real operators set the password
# within seconds, not 30s.
#
# time.monotonic per dispatch: immune to wall-clock drift / NTP step.
_BOOT_MONOTONIC: float = time.monotonic()
_SET_PASSWORD_GRACE_SECONDS: float = 30.0


def _set_password_grace_active() -> bool:
    """True iff the boot-grace window is still open."""
    return (time.monotonic() - _BOOT_MONOTONIC) < _SET_PASSWORD_GRACE_SECONDS


# Bundle B2 / round-9 TOCTOU fix: serialize the set-password
# load->check->save trio across concurrent FastAPI handlers.
#
# Pre-fix, two concurrent POST /api/auth/set-password requests could
# both observe `auth.load() is None`, both compute their own AuthState,
# and atomic_write_text's tmp+rename would be last-write-wins. On a
# fresh-boot device an attacker on the same RFC1918/Tailscale source
# could race the legitimate operator and overwrite the operator's
# password hash with the attacker's.
#
# This Lock is sufficient under the current deploy posture: openmarquee-
# backend.service launches a SINGLE uvicorn worker (no --workers flag).
# If a future deploy moves to multi-worker uvicorn, this single-process
# Lock is no longer sufficient and the fix needs to be at the file-
# system layer (O_CREAT | O_EXCL on a sentinel, or fcntl.flock around
# the load+check+save sequence in AuthStorage itself).
_SET_PASSWORD_LOCK = asyncio.Lock()


AuthDep = Annotated[AuthStorage, Depends(get_auth_storage)]


class _StatusResponse(BaseModel):
    configured: bool


class _SetPasswordRequest(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LEN)
    password_confirm: str


class _LoginRequest(BaseModel):
    password: str


class _ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LEN)
    new_password_confirm: str


class _TokenResponse(BaseModel):
    token: str


@router.get("/status", response_model=_StatusResponse)
async def auth_status(auth: AuthDep) -> _StatusResponse:
    """Unauthenticated. UI uses this to pick between welcome-screen
    (set-password) vs login screen."""
    return _StatusResponse(configured=auth.load() is not None)


@router.post("/set-password", response_model=_TokenResponse)
async def set_password(
    payload: _SetPasswordRequest,
    auth: AuthDep,
    request: Request,
) -> _TokenResponse:
    """First-time password set (welcome flow). 409 once configured --
    subsequent password changes go through /api/auth/change-password
    which requires the current password.

    Bundle B2 item 7: during the first _SET_PASSWORD_GRACE_SECONDS
    after process boot, the endpoint is reachable ONLY from loopback
    / RFC1918 / Tailscale CGNAT. After grace, behavior reverts to
    "reachable from any IP" (the pre-fix posture). Narrows the LAN-
    attacker-races-the-operator window without blocking the
    legitimate operator-on-physical-LAN case.
    """
    if _set_password_grace_active():
        client_host = request.client.host if request.client else None
        if not client_host or not _is_private_or_loopback_ip(client_host):
            raise HTTPException(
                status_code=403,
                detail=(
                    "set-password is restricted to trusted-network sources "
                    "during the post-boot grace window"
                ),
            )
    if payload.password != payload.password_confirm:
        raise HTTPException(status_code=422, detail="passwords do not match")
    from openmarquee.auth import AuthState

    # Round-9 TOCTOU fix: serialize the load->check->save trio so two
    # concurrent set-password POSTs during the grace window can't both
    # observe "not configured" and last-write-wins the rename. See
    # _SET_PASSWORD_LOCK module-level comment for the multi-worker
    # caveat.
    async with _SET_PASSWORD_LOCK:
        if auth.load() is not None:
            raise HTTPException(status_code=409, detail="password already configured")
        state = AuthState(password_hash=hash_password(payload.password))
        token, state = mint_token(state)
        # Persist AFTER mint so the issued_token_hash is on disk before
        # we hand the token back to the client; otherwise a server crash
        # between mint + save would invalidate the just-returned token.
        auth.save(state)
    return _TokenResponse(token=token)


@router.post("/login", response_model=_TokenResponse)
async def login(
    payload: _LoginRequest,
    auth: AuthDep,
    request: Request,
) -> _TokenResponse:
    """Mint a fresh token if the provided password matches. 404 if not
    yet configured (UI should redirect to welcome flow). 401 on wrong
    password. 429 if the source IP has exceeded the rate-limit budget
    (Bundle B2 item 7: 5 attempts per 60s window per IP, fail-closed
    to a shared bucket if the IP can't be identified).

    The throttle gate fires BEFORE the argon2 verify so a hostile
    client can't burn server CPU at the argon2 cost (~250ms/attempt)
    just by knowing the device exists. With uvicorn --proxy-headers
    --forwarded-allow-ips 127.0.0.1 (Bundle B2 piece 1, set in the
    systemd unit) request.client.host correctly reflects the real
    client IP through tailscale-serve; without that config every
    tailscale-served request would share the 127.0.0.1 bucket and
    legitimate operators would be locked out alongside attackers.
    """
    client_ip = client_ip_or_unknown(request.client.host if request.client else None)
    if not LOGIN_BUCKET.try_take(client_ip):
        raise HTTPException(
            status_code=429,
            detail="too many login attempts; try again in a minute",
        )
    state = auth.load()
    if state is None:
        raise HTTPException(status_code=404, detail="password not yet configured")
    if not verify_password(state.password_hash, payload.password):
        raise HTTPException(status_code=401, detail="invalid password")
    token, state = mint_token(state)
    auth.save(state)
    return _TokenResponse(token=token)


@router.post("/change-password", response_model=_TokenResponse)
async def change_password_endpoint(
    payload: _ChangePasswordRequest,
    auth: AuthDep,
    authorization: Annotated[str | None, Header()] = None,
) -> _TokenResponse:
    """Rotate the password. Requires a valid bearer token AND the
    current password. Bumps token_version so all previously-minted
    tokens (including the one the caller just used) stop verifying;
    the returned token is the new one to use going forward."""
    state = auth.load()
    if state is None:
        raise HTTPException(status_code=404, detail="not configured")

    # The middleware should have rejected before we get here, but
    # belt-and-suspenders: don't trust unauth callers past this
    # endpoint specifically. The middleware whitelist excludes this
    # path; only authed callers reach this function.
    token = _strip_bearer(authorization)
    if not await verify_token(token, state):
        raise HTTPException(status_code=401, detail="invalid token")

    if not verify_password(state.password_hash, payload.current_password):
        raise HTTPException(status_code=401, detail="current password is wrong")

    if payload.new_password != payload.new_password_confirm:
        raise HTTPException(status_code=422, detail="new passwords do not match")

    new_state = change_password(state, payload.new_password)
    new_token, new_state = mint_token(new_state)
    auth.save(new_state)
    return _TokenResponse(token=new_token)


def _strip_bearer(authorization: str | None) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header.
    Returns "" when the header is missing or malformed."""
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
