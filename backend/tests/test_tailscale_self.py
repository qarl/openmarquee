"""Unit tests for openmarquee._tailscale_self.

Verifies the 60s TTL cache + the clear_cache() reset hook, plus the
`_query_self` shell-out + parse path (added 2026-05-24 to close
the THIN tier from the backend coverage gap audit). The cache layer
was already pinned; the query layer was relying on the integration
side (FqdnRedirectMiddleware exercising the full path) for coverage
of its individual failure modes.
"""

import json
import subprocess
from unittest.mock import Mock, patch

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
    _query_self -- the whole point of the cache is to spare
    tailscaled the per-request shell-out."""
    fake_query = Mock(return_value=("fireplacesign.tail71c768.ts.net", True))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        first = await _tailscale_self.get_self_fqdn()
        second = await _tailscale_self.get_self_fqdn()
    assert first == "fireplacesign.tail71c768.ts.net"
    assert second == "fireplacesign.tail71c768.ts.net"
    assert fake_query.call_count == 1


@pytest.mark.asyncio
async def test_cache_caches_misses_too():
    """A None response (tailscaled not yet up) must also cache so a
    cold-start backend doesn't shell out on every request while
    tailscale.service is still starting. The 60s TTL bounds the
    "tailscale just came up but middleware doesn't know" window."""
    fake_query = Mock(return_value=(None, False))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        first = await _tailscale_self.get_self_fqdn()
        second = await _tailscale_self.get_self_fqdn()
    assert first is None
    assert second is None
    assert fake_query.call_count == 1


@pytest.mark.asyncio
async def test_clear_cache_forces_refetch():
    """clear_cache() must immediately invalidate any cached value
    so the next get_self_fqdn() re-shells out. Used by tests; also
    handy for an operator-triggered "I just turned on Tailscale,
    refresh now" hook if we ever expose one."""
    fake_query = Mock(side_effect=[("old.ts.net", True), ("new.ts.net", True)])
    with patch.object(_tailscale_self, "_query_self", fake_query):
        first = await _tailscale_self.get_self_fqdn()
        _tailscale_self.clear_cache()
        second = await _tailscale_self.get_self_fqdn()
    assert first == "old.ts.net"
    assert second == "new.ts.net"
    assert fake_query.call_count == 2


# ---- 2026-05-24: extended coverage for _query_self (THIN tier
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
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
        result, _running = _tailscale_self._query_self()
    assert result is None


# ---- 2026-07-16 (qarl): the boot card shows the full Tailscale name when
# Tailscale is ACTIVE, else the .local name. That needs a signal
# `get_self_fqdn` deliberately does not carry, so these pin the split.


@pytest.mark.asyncio
async def test_online_probe_returns_fqdn_when_backend_running():
    fake_query = Mock(return_value=("jasonssign1.tail71c768.ts.net", True))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        assert await _tailscale_self.get_self_fqdn_online() == ("jasonssign1.tail71c768.ts.net")


@pytest.mark.asyncio
async def test_online_probe_is_none_when_backend_not_running():
    """THE GUARD. Self.DNSName SURVIVES in `tailscale status --json` while
    the node is Stopped / NeedsLogin, so keying the card off the FQDN's
    mere presence would advertise a tailnet URL for a sign whose tailnet
    is down -- a URL nobody can reach. Only BackendState says it's up."""
    fake_query = Mock(return_value=("jasonssign1.tail71c768.ts.net", False))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        assert await _tailscale_self.get_self_fqdn_online() is None


@pytest.mark.asyncio
async def test_plain_get_self_fqdn_ignores_backend_state():
    """The middleware's contract must NOT change: FqdnRedirectMiddleware
    wants the canonical name whenever tailscaled knows it, running or
    not. If this ever starts returning None for a Stopped node, the
    redirect silently stops working."""
    fake_query = Mock(return_value=("jasonssign1.tail71c768.ts.net", False))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        assert await _tailscale_self.get_self_fqdn() == ("jasonssign1.tail71c768.ts.net")


def test_cached_read_returns_none_when_cache_is_cold():
    """Cache-only: it must NOT go query. A cold cache is "I don't know",
    which sign_url renders as the honest .local address."""
    fake_query = Mock(return_value=("a.ts.net", True))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        assert _tailscale_self.cached_self_fqdn_online() is None
    assert fake_query.call_count == 0, "the cached read must never spawn a probe"


@pytest.mark.asyncio
async def test_cached_read_serves_a_warm_cache():
    fake_query = Mock(return_value=("a.ts.net", True))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        await _tailscale_self.get_self_fqdn_online()  # warm it off-loop
        assert _tailscale_self.cached_self_fqdn_online() == "a.ts.net"
    assert fake_query.call_count == 1


@pytest.mark.asyncio
async def test_cached_read_respects_the_online_gate():
    fake_query = Mock(return_value=("a.ts.net", False))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        await _tailscale_self.get_self_fqdn_online()
        assert _tailscale_self.cached_self_fqdn_online() is None


def test_there_is_no_blocking_online_accessor():
    """STRUCTURAL. An earlier draft shipped `get_self_fqdn_online_blocking`
    "for the supervisor's own thread" -- but the supervisor has no thread,
    so it would have run a ≤4s subprocess ON the event loop. Not offering
    the call is what stops that coming back; a comment would not."""
    assert not hasattr(_tailscale_self, "get_self_fqdn_online_blocking")


@pytest.mark.asyncio
async def test_both_accessors_share_one_cache():
    """One query answers both questions, so get_self_fqdn and
    get_self_fqdn_online can never disagree about the same tailscaled --
    and the online check costs no extra subprocess."""
    fake_query = Mock(return_value=("a.ts.net", True))
    with patch.object(_tailscale_self, "_query_self", fake_query):
        await _tailscale_self.get_self_fqdn()
        await _tailscale_self.get_self_fqdn_online()
    assert fake_query.call_count == 1


def test_query_parses_backend_state_running():
    """Drive the REAL parse path: a stubbed _query_self cannot prove we
    read BackendState from the actual payload shape."""
    blob = json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "DNSName": "JasonsSign1.tail71c768.ts.net.",
                "TailscaleIPs": ["100.101.102.103"],
            },
        }
    )
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, blob, ""),
        ),
    ):
        fqdn, running = _tailscale_self._query_self()
    assert fqdn == "jasonssign1.tail71c768.ts.net", "trailing dot stripped + lowercased"
    assert running is True


def test_query_parses_backend_state_stopped():
    blob = json.dumps(
        {
            "BackendState": "Stopped",
            "Self": {
                "DNSName": "jasonssign1.tail71c768.ts.net.",
                "TailscaleIPs": ["100.101.102.103"],
            },
        }
    )
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, blob, ""),
        ),
    ):
        fqdn, running = _tailscale_self._query_self()
    assert fqdn == "jasonssign1.tail71c768.ts.net", "the name is still known..."
    assert running is False, "...but the node is NOT up, so the card must not use it"


def _status_blob(**self_overrides):
    self_node = {
        "DNSName": "jasonssign1.tail71c768.ts.net.",
        "TailscaleIPs": ["100.101.102.103"],
    }
    self_node.update(self_overrides)
    return json.dumps({"BackendState": "Running", "Self": self_node})


def _run_query(blob):
    with (
        patch.object(_tailscale_self.shutil, "which", return_value="/usr/bin/tailscale"),
        patch.object(
            _tailscale_self.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, blob, ""),
        ),
    ):
        return _tailscale_self._query_self()


def test_running_without_a_tailnet_ip_is_not_online():
    """Running + no assigned TailscaleIP resolves to NOTHING, so the card
    would print a URL that goes nowhere. network_supervisor_takeover's
    preflight pairs Running with a non-empty TailscaleIPs for the same
    reason; this follows it."""
    fqdn, running = _run_query(_status_blob(TailscaleIPs=[]))
    assert fqdn == "jasonssign1.tail71c768.ts.net"
    assert running is False


def test_running_with_an_expired_key_is_not_online():
    """An expired node still appears in the netmap but refuses traffic."""
    fqdn, running = _run_query(_status_blob(Expired=True))
    assert fqdn == "jasonssign1.tail71c768.ts.net"
    assert running is False


def test_running_with_ip_and_unexpired_key_is_online():
    """Control for the two tests above: proves they fail for their OWN
    reason and the gate isn't just stuck returning False."""
    fqdn, running = _run_query(_status_blob())
    assert fqdn == "jasonssign1.tail71c768.ts.net"
    assert running is True
