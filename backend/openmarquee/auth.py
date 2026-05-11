"""Password-based bearer-token auth for the captive-portal HTTP API
(Batch 20.1 / phase A.1).

The operator sets a password at first-boot welcome flow. That password
becomes the credential for all subsequent login. No usernames -- the
device is single-tenant; "logged in" means "knows the password".

Token shape:
    `<token_version>.<32-byte-urlsafe-base64-secret>`

`token_version` is a monotonically-increasing counter stored on the
device alongside the password hash. Bumping it on password change
invalidates every previously-issued token without needing a token
database. Tokens are stateless from the server's perspective: verify
keys on (a) the version prefix matches the stored version, (b) the
secret part is the right shape. The secret part is *not* validated
against a stored value because we'd need a per-token database for
that; the version prefix is the rotation lever instead.

Threat model:
- Operator-trusted physical access to the SD card: out of scope
  (delete /var/openmarquee/auth.json + redo welcome flow).
- Captive-portal WiFi access without the password: blocked.
- Network sniffing the AP: HTTP over the captive portal is open, so
  the token DOES go in cleartext. SYSTEM_SPEC §4 acknowledges this
  -- the AP is for one-time setup, after which the device is reachable
  via Tailscale (which does its own E2E encryption + auth).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import BaseModel, Field, ValidationError

from openmarquee._atomic import atomic_write_text
from openmarquee._storage_recovery import quarantine_corrupt_file

log = logging.getLogger(__name__)

# Bump when AuthState fields gain non-backward-compat changes.
AUTH_SCHEMA_VERSION = 1

# Argon2id-defaults via argon2-cffi's PasswordHasher (time_cost=3,
# memory_cost=64 MiB, parallelism=4). Verifies in ~50ms on Pi Zero 2 W
# from a cold cache -- the per-request overhead is fine because login
# is rare relative to authed API calls (those just compare a token
# prefix, no argon2 work).
_HASHER = PasswordHasher()

# Minimum password length the operator can set. 8 is the conventional
# floor; no max length (argon2 truncates internally if needed).
MIN_PASSWORD_LEN = 8


class AuthState(BaseModel):
    """On-disk persisted auth configuration.

    `password_hash` is an argon2id PHC string ($argon2id$v=19$m=...$$).
    Self-describing -- the params live inside the string so a future
    PasswordHasher() default tweak still verifies older hashes.

    `token_version` starts at 1 and increments on every password
    change. Tokens minted at version N stop verifying once the version
    advances to N+1 (`mint_token` prefixes the version, `verify_token`
    checks for exact match).
    """

    schema_version: int = Field(default=AUTH_SCHEMA_VERSION)
    password_hash: str
    token_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthStorage:
    """File-backed AuthState persistence. Mirrors SettingsStorage's
    shape (atomic-write 0600 + corruption-recovery)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> AuthState | None:
        """Return the stored AuthState, or None if not yet configured.

        On corrupt JSON or schema mismatch: quarantine the bad file +
        WARN + return None. The operator's recovery path is the same
        as not-configured (welcome flow), which is consistent with the
        forgot-password documentation (delete auth.json + redo flow).
        """
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
            return AuthState.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            quarantine_corrupt_file(self.path, exc)
            return None

    def save(self, state: AuthState) -> None:
        """Persist AuthState. atomic_write_text sets 0600 (Batch 11.2)
        -- the password_hash is sensitive even though argon2id makes
        offline cracking expensive."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, state.model_dump_json(indent=2))


# --- password hashing ---


def hash_password(plaintext: str) -> str:
    """Hash via argon2id. Returns the PHC string (self-describing
    params + salt + hash). The returned string is what goes into
    AuthState.password_hash."""
    return _HASHER.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """Constant-time compare via argon2-cffi's verify. Returns False
    on any error -- argon2-cffi raises VerifyMismatchError for the
    wrong-password case; we also catch the parse / invalid-hash
    branches so a corrupt or absent hash never raises into the API
    layer."""
    try:
        _HASHER.verify(stored_hash, plaintext)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        # Invalid hash format, hash params unsupported, etc. Treat as
        # auth failure -- the operator's recovery is delete + redo.
        log.warning("verify_password: unexpected error", exc_info=True)
        return False


# --- token mint + verify ---


def mint_token(state: AuthState) -> str:
    """Return a fresh token bound to the current token_version. Shape
    is `<version>.<32-byte-urlsafe>`. The version prefix is the
    rotation lever -- mint_token is called on set-password / login /
    change-password; the resulting token verifies against the CURRENT
    version, so a change-password call's token bump invalidates
    every previously-issued token at the next verify_token call."""
    secret = secrets.token_urlsafe(32)
    return f"{state.token_version}.{secret}"


def verify_token(token: str, state: AuthState | None) -> bool:
    """True iff `token` is well-formed AND its version prefix matches
    the stored state's current `token_version`.

    Returns False when:
      - state is None (auth not configured -- nothing to verify against)
      - token is empty / malformed (no dot, wrong-shape secret part)
      - version prefix doesn't parse as int
      - version prefix doesn't match state.token_version

    Note: the secret part is NOT compared against any stored value --
    the stateless design is that the server trusts any token whose
    version matches. This makes mint_token cheap (no DB write) and
    makes change-password invalidate-all trivial (bump version), at
    the cost of not being able to revoke an individual token. The
    captive-portal threat model doesn't need individual revocation.
    """
    if state is None or not token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    version_str, secret = parts
    try:
        version = int(version_str)
    except ValueError:
        return False
    # Secret part must be 43 chars (token_urlsafe(32) -> 43-char b64).
    # Tighten the shape so a "version.x" can't slip through.
    if len(secret) != 43:
        return False
    return version == state.token_version


def change_password(state: AuthState, new_plaintext: str) -> AuthState:
    """Return a new AuthState with the rehashed password + bumped
    token_version + refreshed updated_at. Caller persists via
    AuthStorage.save."""
    return state.model_copy(
        update={
            "password_hash": hash_password(new_plaintext),
            "token_version": state.token_version + 1,
            "updated_at": datetime.now(UTC),
        }
    )
