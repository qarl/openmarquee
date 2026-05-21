"""Unit tests for bearer-token resolution + persistence."""

import os
import stat

from openmarquee_web_helper.token import ENV_VAR, resolve_token


def test_env_var_token_takes_precedence(monkeypatch, tmp_path):
    """An OPENMARQUEE_WEB_HELPER_TOKEN env var wins; no file is written."""
    monkeypatch.setenv(ENV_VAR, "env-supplied-token")
    token_file = tmp_path / "token"

    assert resolve_token(token_file) == "env-supplied-token"
    assert not token_file.exists()


def test_token_file_used_when_no_env_var(monkeypatch, tmp_path):
    """With no env var, an existing token file is read."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("file-supplied-token\n")

    assert resolve_token(token_file) == "file-supplied-token"


def test_token_generated_and_persisted_with_0600(monkeypatch, tmp_path):
    """With no env var and no file, a token is generated, saved 0600,
    and is stable across a second resolve (restart)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    token_file = tmp_path / "sub" / "token"

    first = resolve_token(token_file)
    assert first  # non-empty random token
    assert token_file.exists()

    # Persisted at mode 0600 -- owner read/write only.
    mode = stat.S_IMODE(os.stat(token_file).st_mode)
    assert mode == 0o600

    # Stable across "restart" -- the same token is loaded from the file.
    second = resolve_token(token_file)
    assert second == first
