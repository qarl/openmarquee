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

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

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

router = APIRouter(prefix="/api/auth", tags=["auth"])

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
) -> _TokenResponse:
    """First-time password set (welcome flow). 409 once configured --
    subsequent password changes go through /api/auth/change-password
    which requires the current password."""
    if payload.password != payload.password_confirm:
        raise HTTPException(status_code=422, detail="passwords do not match")
    if auth.load() is not None:
        raise HTTPException(
            status_code=409, detail="password already configured"
        )
    from openmarquee.auth import AuthState

    state = AuthState(password_hash=hash_password(payload.password))
    auth.save(state)
    return _TokenResponse(token=mint_token(state))


@router.post("/login", response_model=_TokenResponse)
async def login(
    payload: _LoginRequest,
    auth: AuthDep,
) -> _TokenResponse:
    """Mint a fresh token if the provided password matches. 404 if not
    yet configured (UI should redirect to welcome flow). 401 on wrong
    password."""
    state = auth.load()
    if state is None:
        raise HTTPException(
            status_code=404, detail="password not yet configured"
        )
    if not verify_password(state.password_hash, payload.password):
        raise HTTPException(status_code=401, detail="invalid password")
    return _TokenResponse(token=mint_token(state))


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
    if not verify_token(token, state):
        raise HTTPException(status_code=401, detail="invalid token")

    if not verify_password(state.password_hash, payload.current_password):
        raise HTTPException(
            status_code=401, detail="current password is wrong"
        )

    if payload.new_password != payload.new_password_confirm:
        raise HTTPException(
            status_code=422, detail="new passwords do not match"
        )

    new_state = change_password(state, payload.new_password)
    auth.save(new_state)
    return _TokenResponse(token=mint_token(new_state))


def _strip_bearer(authorization: str | None) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header.
    Returns "" when the header is missing or malformed."""
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
