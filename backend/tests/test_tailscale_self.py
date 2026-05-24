"""Unit tests for openmarquee._tailscale_self.

Verifies the 60s TTL cache + the clear_cache() reset hook. We don't
exercise the real `tailscale status --json` subprocess here -- the
parsing logic mirrors api_flock._discover_tailnet_candidates which is
already covered by its own tests; this file pins the cache contract.
"""

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
