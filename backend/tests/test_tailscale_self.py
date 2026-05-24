"""Unit tests for openmarquee._tailscale_self.

Verifies the 60s TTL cache + the clear_cache() reset hook, plus the
`_query_self_fqdn` shell-out + parse path (added 2026-05-24 to close
the THIN tier from the backend coverage gap audit). The cache layer
was already pinned; the query layer was relying on the integration
side (FqdnRedirectMiddleware exercising the full path) for coverage
of its individual failure modes.
"""

import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from openmarquee import _tailscale_self


@pytest.fixture(autouse=True)
def _reset_cache():
    """Drop any cache state between tests so a hit from the prior
    test doesn't leak into this one. Pytest's per-test isolation
    only resets fixtures, not module-level state."""
    _tailscale_self.clear_cache()
    yield
    _tailscale_self.clear_cache()


@pytest.mark.asyncio
async def test_cache_hit_avoids_second_subprocess_call():
    """Second call within the TTL window must NOT re-invoke
    _query_self_fqdn -- the whole point of the cache is to spare
    tailscaled the per-request shell-out."""
    fake_query = AsyncMock(return_value="fireplacesign.tail71c768.ts.net")
    with patch.object(_tailscale_self, "_query_self_fqdn", fake_query):
        first = await _tailscale_self.get_self_fqdn()
        second = await _tailscale_self.get_self_fqdn()
    assert first == "fireplacesign.tail71c768.ts.net"
    assert second == "fireplacesign.tail71c768.ts.net"
    assert fake_query.await_count == 1


@pytest.mark.asyncio
async def test_cache_caches_misses_too():
    """A None response (tailscaled not yet up) must also cache so a
    cold-start backend doesn't shell out on every request while
    tailscale.service is still starting. The 60s TTL bounds the
    "tailscale just came up but middleware doesn't know" window."""
    fake_query = AsyncMock(return_value=None)
    with patch.object(_tailscale_self, "_query_self_fqdn", fake_query):
        first = await _tailscale_self.get_self_fqdn()
        second = await _tailscale_self.get_self_fqdn()
    assert first is None
    assert second is None
    assert fake_query.await_count == 1


@pytest.mark.asyncio
async def test_clear_cache_forces_refetch():
    """clear_cache() must immediately invalidate any cached value
    so the next get_self_fqdn() re-shells out. Used by tests; also
    handy for an operator-triggered "I just turned on Tailscale,
    refresh now" hook if we ever expose one."""
    fake_query = AsyncMock(side_effect=["old.ts.net", "new.ts.net"])
    with patch.object(_tailscale_self, "_query_self_fqdn", fake_query):
        first = await _tailscale_self.get_self_fqdn()
        _tailscale_self.clear_cache()
        second = await _tailscale_self.get_self_fqdn()
    assert first == "old.ts.net"
    assert second == "new.ts.net"
    assert fake_query.await_count == 2


# ---- 2026-05-24: extended coverage for _query_self_fqdn (THIN tier
# ---- closure). The cache layer above already pinned the read-side
# ---- contract; this block pins the subprocess + parse side.


def _fake_completed_process(stdout: str = "", returncode: int = 0):
    """Build a stand-in for subprocess.run's return value.

    subprocess.CompletedProcess is a regular dataclass-shaped class so
    constructing one directly is cleaner than a Mock spec'd against it.
    """
    return subprocess.CompletedProcess(
        args=["tailscale", "status", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def _build_self_payload(dns_name: str | None) -> str:
    """Minimal tailscale-status-shape JSON with just the field we read."""
    self_node: dict = {}
    if dns_name is not None:
        self_node["DNSName"] = dns_name
    return json.dumps({"Self": self_node})


@pytest.mark.asyncio
async def test_query_returns_none_when_tailscale_binary_missing():
    """If `tailscale` isn't on PATH (e.g. dev box without Tailscale
    installed, or production Pi pre-tailscaled-install), the helper
    must short-circuit to None without raising. Avoids a spurious
    FileNotFoundError that would crash the middleware."""
    with patch.object(_tailscale_self.shutil, "which", return_value=None):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None


@pytest.mark.asyncio
async def test_query_returns_none_on_subprocess_timeout():
    """The 4s timeout matches api_flock's same gate. On a slow-to-
    respond tailscaled (just-started, network thrash, transient hang),
    we'd rather get None + log + retry-next-tick than block the
    HTTP-request thread waiting indefinitely."""
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=4),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None


@pytest.mark.asyncio
async def test_query_returns_none_on_oserror_spawning():
    """An OSError at spawn time (e.g. PATH lookup raced with a binary
    removal, exec-format error from a corrupted install) must also
    return None — the helper's `except (TimeoutExpired, OSError)`
    handles both spawn-fail and timeout uniformly."""
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            side_effect=OSError("exec format error"),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None


@pytest.mark.asyncio
async def test_query_returns_none_on_nonzero_exit():
    """tailscale exiting non-zero (e.g. `tailscale status` before login,
    or against a daemon that's running but unhealthy) returns None
    without trying to parse the (likely empty) stdout."""
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=_fake_completed_process(stdout="", returncode=1),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None


@pytest.mark.asyncio
async def test_query_returns_none_on_non_json_stdout():
    """A future tailscale version that changes the output format, or
    a wrapper script injecting a warning line before the JSON, would
    produce stdout that doesn't parse as JSON. Helper logs + returns
    None rather than raising JSONDecodeError up the stack."""
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=_fake_completed_process(stdout="not valid json"),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None


@pytest.mark.parametrize(
    "self_value",
    [
        None,  # missing top-level Self key entirely
        "not-a-dict",  # Self present but wrong type (string)
        [],  # Self present but wrong type (list)
        42,  # Self present but wrong type (number)
    ],
    ids=["missing", "string", "list", "number"],
)
@pytest.mark.asyncio
async def test_query_returns_none_when_self_missing_or_wrong_shape(self_value):
    """The helper's `isinstance(self_node, dict)` guard catches a
    future tailscale schema change that drops Self or replaces it
    with a different shape. Without the guard, `.get("DNSName")`
    would raise AttributeError on a non-dict value."""
    payload = json.dumps({"Self": self_value} if self_value is not None else {})
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=_fake_completed_process(stdout=payload),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None


@pytest.mark.asyncio
async def test_query_strips_trailing_dot_and_lowercases_dnsname():
    """tailscale's Self.DNSName carries a trailing dot (DNS root) and
    may surface mixed-case characters depending on tailnet config.
    Cross-reference with api_flock._discover_tailnet_candidates
    requires case-stable lowercase + no trailing dot — this is the
    normalization the helper guarantees."""
    payload = _build_self_payload("FirePlaceSign.Tail71C768.TS.NET.")
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=_fake_completed_process(stdout=payload),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result == "fireplacesign.tail71c768.ts.net"


@pytest.mark.parametrize(
    "dns_name",
    ["", " ", "   ", "\t", "\n"],
    ids=["empty", "single-space", "spaces", "tab", "newline"],
)
@pytest.mark.asyncio
async def test_query_returns_none_for_empty_or_whitespace_dnsname(dns_name):
    """An empty or whitespace-only DNSName (cold tailscaled, just-
    initialized node before the first tailnet handshake) collapses to
    "" after the strip/rstrip/lower chain — the final `or None` then
    returns None so the caller treats it as "no FQDN available" rather
    than caching an empty string."""
    payload = _build_self_payload(dns_name)
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=_fake_completed_process(stdout=payload),
        ),
    ):
        result = await _tailscale_self._query_self_fqdn()
    assert result is None
