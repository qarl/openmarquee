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

import asyncio
import json
import logging
import secrets
import time
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

# Argon2id parameters tuned to the OWASP "Password Storage Cheat Sheet"
# floor for argon2id (m=19456 KiB = 19 MiB, t=2, p=1). This is the modern
# security minimum -- anything stronger is over-spec relative to what
# Pi Zero 2 W can serve in real time, anything weaker drops below the
# OWASP recommendation.
#
# Measured on the dev Pi Zero 2 W (Cortex-A53 4x @ 1GHz, 512MB RAM,
# Bookworm trixie, Python 3.13, argon2-cffi 25.1.0) -- 2026-05-11:
#   library defaults (t=3 m=64MiB p=4):  median 779ms, max 2055ms
#   OWASP min (t=2 m=19MiB p=1):         median 249ms, max 253ms
# The defaults miss the <500ms login-latency target the operator
# experience needs; more importantly, parallelism=4 thrashes the Pi's
# weak cores and produces 2s worst-case logins that read as broken to
# the operator. parallelism=1 stabilises variance (10x tighter range)
# alongside hitting the latency target.
#
# Do NOT "improve" these params back to library defaults without
# re-measuring on hardware -- the floor is set by OWASP, not by
# what the library happens to ship as defaults.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
)

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

    `issued_token_hash` is the argon2id hash of the most-recently-
    minted token's secret part (2026-05-24 security audit finding 1,
    Path A). Each mint OVERWRITES the prior hash for the current
    version -- only the latest-issued token verifies. Operator-visible
    consequence: logging in on a second device invalidates the first
    device's session. Single-tenant device + the operator UI shows a
    "your session ended" path on the next request; acceptable trade-
    off for the no-database design. None on a freshly-saved AuthState
    before the first mint; populated by `mint_token`.
    """

    schema_version: int = Field(default=AUTH_SCHEMA_VERSION)
    password_hash: str
    token_version: int = Field(default=1, ge=1)
    issued_token_hash: str | None = Field(default=None)
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


def mint_token(state: AuthState) -> tuple[str, AuthState]:
    """Return (fresh_token, updated_state) where the new state carries
    an argon2 hash of the token's secret part.

    Shape: `<version>.<32-byte-urlsafe-secret>`. The version prefix is
    the rotation lever; the secret part is what verify_token argon2-
    verifies against state.issued_token_hash. Each mint OVERWRITES
    the previous hash -- the prior-issued token stops verifying as
    soon as the new state is persisted.

    Callers MUST persist the returned state via AuthStorage.save()
    before returning the token to the client; otherwise a server
    restart between mint + the first verify would lose the hash and
    invalidate the just-minted token.

    The argon2 hash is a CPU-bound operation (~250ms on Pi Zero 2 W
    per the measured OWASP-min params). Mint is async-naive (sync
    function) because mint sites are typically operator-driven (login,
    password-change) where the ~250ms is part of the normal login
    latency budget. Verify is async-wrapped (see verify_token).

    The verified-token cache is flushed on every mint: a re-mint
    under the same version overwrites issued_token_hash, which means
    any previously-cached entry for a now-invalid token would still
    short-circuit positive for up to its TTL. Flushing on mint
    closes that window (subagent code review 2026-05-24).
    """
    clear_verified_token_cache()
    secret = secrets.token_urlsafe(32)
    secret_hash = _HASHER.hash(secret)
    updated = state.model_copy(
        update={
            "issued_token_hash": secret_hash,
            "updated_at": datetime.now(UTC),
        }
    )
    return f"{state.token_version}.{secret}", updated


# ---- Verified-token cache (2026-05-24 security audit finding 1 Path A) ----
#
# argon2.verify costs ~250ms on Pi Zero 2 W (OWASP-min params). Every
# HTTP request running through the auth middleware would block 250ms
# without this cache. The cache amortizes that to "first request per
# unique token per 10-min window pays the argon2 cost; subsequent
# requests are sub-µs dict lookups."
#
# Keyed by the full bearer token string (the same value the client
# sends on every request). TTL is 10min (middle of the 5-15min range
# QA's dispatch specified). Lazy expiration on read -- no background
# sweeper, no cleanup-task lifecycle to manage.
#
# Cache invalidation: clear_verified_token_cache() is called by
# change_password() so a token-version bump can't leave a stale
# "previously verified" entry behind. On process restart the dict is
# empty -- everyone re-verifies once, acceptable.
_VERIFIED_TOKEN_CACHE: dict[str, float] = {}
_VERIFIED_TOKEN_TTL_SECONDS = 10 * 60  # 10 min


def _argon2_verify_secret(stored_hash: str, secret: str) -> bool:
    """Module-level wrapper around _HASHER.verify so tests can
    monkeypatch the argon2 call site without monkeypatching the
    PasswordHasher class itself (whose methods are read-only on the
    instance). Returns True on success; raises VerifyMismatchError on
    mismatch (matches the underlying argon2-cffi contract)."""
    _HASHER.verify(stored_hash, secret)
    return True


def clear_verified_token_cache() -> None:
    """Drop every cached verified-token entry. Called by
    change_password so previously-verified-but-now-rotated tokens
    don't get a free pass via the cache. Tests also use this to
    isolate cases."""
    _VERIFIED_TOKEN_CACHE.clear()


async def verify_token(token: str, state: AuthState | None) -> bool:
    """True iff `token` is well-formed, its version prefix matches the
    stored state's current `token_version`, AND its secret part argon2-
    verifies against `state.issued_token_hash` (constant-time-compared
    by argon2-cffi).

    Returns False when:
      - state is None (auth not configured)
      - state.issued_token_hash is None (mint hasn't been persisted yet)
      - token is empty / malformed (no dot, wrong-shape secret part)
      - version prefix doesn't parse as int
      - version prefix doesn't match state.token_version
      - argon2-verify of secret vs issued_token_hash fails

    Cache: a positive verify result is cached for 10min, keyed by the
    full bearer token string. Subsequent calls with the same token
    short-circuit to a dict lookup (sub-µs). Cache invalidates on
    change_password via clear_verified_token_cache().

    Async: argon2.verify is wrapped via asyncio.to_thread to keep the
    event loop responsive -- a single ~250ms argon2 call would
    otherwise block every other concurrent request.
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
    if len(secret) != 43:
        return False
    if version != state.token_version:
        return False
    if state.issued_token_hash is None:
        return False
    # Fast cache lookup before the ~250ms argon2 verify. The cache key
    # is the full bearer string (version.secret) so a rotation that
    # changes the hash but reuses the version (impossible by design,
    # since mint always issues a fresh secret) still wouldn't allow a
    # stale cache hit to validate a wrong secret -- the secret is part
    # of the key.
    now = time.monotonic()
    cached_expiry = _VERIFIED_TOKEN_CACHE.get(token)
    if cached_expiry is not None:
        if cached_expiry >= now:
            return True
        # Expired -- drop it lazily so the dict doesn't grow forever.
        _VERIFIED_TOKEN_CACHE.pop(token, None)
    # argon2.verify is CPU-bound; offload to a thread so the event
    # loop stays responsive. The thread pool default (min(32, cpu+4))
    # is fine; concurrent verifies stack up to that limit.
    try:
        ok = await asyncio.to_thread(_argon2_verify_secret, state.issued_token_hash, secret)
    except VerifyMismatchError:
        return False
    except Exception:
        # Corrupt issued_token_hash (PHC parse failure), unsupported
        # params, etc. Fail-closed -- the operator's recovery is the
        # same as for any corrupt auth state.
        log.warning("verify_token: unexpected argon2 error", exc_info=True)
        return False
    if not ok:
        # argon2-cffi raises on mismatch; reaching here with ok=False
        # means a bool-returning shim somewhere. Defensive: treat as
        # mismatch.
        return False
    _VERIFIED_TOKEN_CACHE[token] = now + _VERIFIED_TOKEN_TTL_SECONDS
    return True


def change_password(state: AuthState, new_plaintext: str) -> AuthState:
    """Return a new AuthState with the rehashed password + bumped
    token_version + refreshed updated_at + cleared issued_token_hash.
    Caller persists via AuthStorage.save AND must mint_token() against
    the new state to issue a token (the cleared hash means verify
    against any prior token returns False until the next mint).

    Also clears the verified-token cache so previously-verified-but-
    now-rotated tokens can't ride out the TTL window."""
    clear_verified_token_cache()
    return state.model_copy(
        update={
            "password_hash": hash_password(new_plaintext),
            "token_version": state.token_version + 1,
            "issued_token_hash": None,
            "updated_at": datetime.now(UTC),
        }
    )
