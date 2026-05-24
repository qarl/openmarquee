"""Resolve the device's own Tailscale FQDN at runtime.

Sibling helper to `openmarquee.api_flock._discover_tailnet_candidates`
(api_flock.py:319-375): same `tailscale status --json` shell-out, but
reads the `Self.DNSName` field (the local node's FQDN) instead of the
`Peer.*.DNSName` table (peer FQDNs).

Used by FqdnRedirectMiddleware to 301-redirect non-FQDN requests to the
canonical `https://<self-fqdn>/` URL. The middleware injects this
resolver, so tests bypass the subprocess entirely with a fake.

Auto-detection (over an operator-set settings field) means the redirect
target tracks tailnet rename / re-key without operator intervention.

Cache: 60s TTL per-process. tailscaled boots AFTER the backend per
systemd ordering, so the FQDN may be None on the first lookup window
and become available after tailscaled finishes its handshake. The TTL
+ "None responses are also cached" semantics mean a cold-start backend
re-tries every 60s until the FQDN appears, then caches the hit.

The middleware never calls this from a hot inner loop -- it's bounded
by HTTP-request rate, capped by the cache, and the underlying
subprocess has a 4s timeout (matching api_flock.py's same gate).
"""

import asyncio
import json
import logging
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, object] = {"fqdn": None, "expires_at": 0.0}


async def get_self_fqdn() -> str | None:
    """Return the local node's Tailscale FQDN, or None if unavailable.

    Returns the lowercased FQDN (matching the normalization in
    api_flock._discover_tailnet_candidates so cross-reference stays
    case-stable). None when:
    - the `tailscale` binary isn't on PATH
    - `tailscale status --json` times out / errors / returns non-JSON
    - the parsed payload has no Self.DNSName

    Caches both hits AND misses for 60s so a cold-start backend with
    tailscaled not yet up doesn't shell out on every request.
    """
    now = time.monotonic()
    if now < float(_cache["expires_at"]):
        return _cache["fqdn"]  # type: ignore[return-value]

    fqdn = await _query_self_fqdn()
    _cache["fqdn"] = fqdn
    _cache["expires_at"] = now + _CACHE_TTL_SECONDS
    return fqdn


def clear_cache() -> None:
    """Drop the cached FQDN so the next lookup re-queries tailscaled.

    Provided for tests that want a fresh lookup window without waiting
    out the 60s TTL. Production code should not need to call this.
    """
    _cache["fqdn"] = None
    _cache["expires_at"] = 0.0


async def _query_self_fqdn() -> str | None:
    if not shutil.which("tailscale"):
        return None
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        log.warning("tailscale status probe timed out / failed to spawn")
        return None
    if out.returncode != 0:
        return None
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        log.warning("tailscale status returned non-JSON stdout")
        return None
    self_node = payload.get("Self")
    if not isinstance(self_node, dict):
        return None
    dns_name = (self_node.get("DNSName") or "").strip().rstrip(".").lower()
    return dns_name or None
