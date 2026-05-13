"""Tests for backend/openmarquee/tailscale.py.

Stubs the `tailscale` binary via $OPENMARQUEE_TAILSCALE_BIN so the
suite doesn't need a real Tailscale install. The stub is a bash
script that emits the expected stdout/stderr shape for each
sub-command, exits on time.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from openmarquee import tailscale


def _write_stub(tmp_path: Path, script_body: str) -> Path:
    """Write a bash stub at tmp_path/tailscale and chmod +x. Returns
    the path so the test can `monkeypatch.setenv(...)`."""
    path = tmp_path / "tailscale"
    path.write_text("#!/usr/bin/env bash\n" + script_body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stub_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an empty tmp_path; tests write the stub themselves."""
    return tmp_path


@pytest.mark.asyncio
async def test_start_up_parses_auth_url(stub_dir, monkeypatch):
    """Happy path: tailscale up prints the auth URL to stderr, then
    blocks. read_up returns the URL with state=pending."""
    stub = _write_stub(
        stub_dir,
        # Emit a realistic URL to stderr, then sleep so the process
        # is still alive when start_up tries to detach.
        "echo 'To authenticate, visit:' >&2\n"
        "echo '  https://login.tailscale.com/a/abc123def456' >&2\n"
        "sleep 30\n",
    )
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.start_up(device_id="MySign7K2")
    assert result["state"] == "pending"
    assert result["auth_url"] == "https://login.tailscale.com/a/abc123def456"


@pytest.mark.asyncio
async def test_start_up_returns_error_when_binary_missing(monkeypatch):
    """Off-device dev / Tailscale not installed: return state=error
    with a useful message instead of crashing."""
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", "/nonexistent/tailscale")
    result = await tailscale.start_up(device_id="MySign7K2")
    assert result["state"] == "error"
    assert "tailscale binary not found" in result["message"]


@pytest.mark.asyncio
async def test_start_up_returns_error_when_no_url_appears(stub_dir, monkeypatch):
    """tailscale binary returns immediately without printing a URL
    (broken install, daemon restart loop, etc.). read_up returns
    state=error."""
    stub = _write_stub(stub_dir, "echo 'unexpected output' >&2\nexit 0\n")
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.start_up(device_id="MySign7K2")
    assert result["state"] == "error"
    assert "auth URL" in result["message"]


@pytest.mark.asyncio
async def test_read_status_authenticated(stub_dir, monkeypatch):
    """tailscale status --json emits BackendState=Running + Self
    has TailscaleIPs."""
    status_json = json.dumps({
        "BackendState": "Running",
        "Self": {
            "HostName": "mysign7k2",
            "TailscaleIPs": ["100.64.1.2", "fd7a:115c::1"],
        },
    })
    stub = _write_stub(stub_dir, f"cat <<'EOF'\n{status_json}\nEOF\n")
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.read_status()
    assert result["state"] == "authenticated"
    assert result["hostname"] == "mysign7k2"
    assert result["ipv4"] == "100.64.1.2"


@pytest.mark.asyncio
async def test_read_status_needs_login(stub_dir, monkeypatch):
    """BackendState=NeedsLogin: state="pending"."""
    status_json = json.dumps({"BackendState": "NeedsLogin", "Self": {}})
    stub = _write_stub(stub_dir, f"cat <<'EOF'\n{status_json}\nEOF\n")
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.read_status()
    assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_read_status_stopped_is_disabled(stub_dir, monkeypatch):
    status_json = json.dumps({"BackendState": "Stopped", "Self": {}})
    stub = _write_stub(stub_dir, f"cat <<'EOF'\n{status_json}\nEOF\n")
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.read_status()
    assert result["state"] == "disabled"


@pytest.mark.asyncio
async def test_read_status_returns_error_on_bad_json(stub_dir, monkeypatch):
    stub = _write_stub(stub_dir, "echo 'not json'\n")
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.read_status()
    assert result["state"] == "error"


@pytest.mark.asyncio
async def test_read_status_returns_error_when_binary_missing(monkeypatch):
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", "/nonexistent/tailscale")
    result = await tailscale.read_status()
    assert result["state"] == "error"


@pytest.mark.asyncio
async def test_auth_url_regex_tolerant_of_whitespace(stub_dir, monkeypatch):
    """Tailscale's output format has varied across versions: some
    print "Browser:\\nURL" inline; others print "visit:\\n  URL"
    with leading whitespace. The regex matches the URL itself."""
    stub = _write_stub(
        stub_dir,
        "echo '   https://login.tailscale.com/a/with-leading-whitespace' >&2\n"
        "sleep 30\n",
    )
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    result = await tailscale.start_up(device_id=None)
    assert result["state"] == "pending"
    assert result["auth_url"] == "https://login.tailscale.com/a/with-leading-whitespace"
