"""Shared-bearer-token resolution + persistence for the Web slide helper.

The helper protects `/shot` with a static `Authorization: Bearer <token>`
header. The operator copies the token into the sign's Web slide settings.

Token resolution order at startup:
  1. The `OPENMARQUEE_WEB_HELPER_TOKEN` env var, if set.
  2. A token file (`~/.openmarquee-web-helper/token`), if it exists.
  3. Otherwise: generate a strong random token, persist it to that file
     (mode 0600) and use it.

Persisting the generated token keeps it stable across restarts so a
non-expert operator does not have to re-paste it every time the helper
is bounced.
"""

import os
import secrets
import stat
from pathlib import Path

ENV_VAR = "OPENMARQUEE_WEB_HELPER_TOKEN"

# Per-user state dir for the persisted token. Kept out of the repo /
# cwd so it survives upgrades and re-deploys.
DEFAULT_TOKEN_DIR = Path.home() / ".openmarquee-web-helper"
DEFAULT_TOKEN_FILE = DEFAULT_TOKEN_DIR / "token"


def _read_token_file(path: Path) -> str | None:
    """Return the token stored in `path`, or None if it is absent/empty."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return text or None


def _write_token_file(path: Path, token: str) -> None:
    """Persist `token` to `path` with 0600 perms, creating the dir as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create restrictively, then write -- avoids a brief window where the
    # file exists world-readable.
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def resolve_token(token_file: Path = DEFAULT_TOKEN_FILE) -> str:
    """Resolve the active bearer token (see module docstring for order).

    Generates + persists a new token only when neither the env var nor a
    token file supplies one.
    """
    env_token = os.environ.get(ENV_VAR, "").strip()
    if env_token:
        return env_token

    file_token = _read_token_file(token_file)
    if file_token:
        return file_token

    token = secrets.token_urlsafe(32)
    _write_token_file(token_file, token)
    return token
