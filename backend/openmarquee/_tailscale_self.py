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
_cache: dict[str, object] = {"fqdn": None, "running": False, "expires_at": 0.0}

_BACKEND_STATE_RUNNING = "Running"
"""tailscaled's `BackendState` when the node is up on the tailnet.

`Running` ALONE is not enough to say the sign is reachable at its FQDN,
so the online check also requires a assigned tailnet IP and a
non-expired node key. `network_supervisor_takeover.parse_tailscale_
status_json` already pairs Running with a non-empty `Self.TailscaleIPs`
for the same reason -- it's the stricter and more reachability-honest of
the two existing readings, so this follows it rather than
`tailscale.read_status`, which maps Running -> "authenticated" for a
UI status pill where "up but no IP yet" is a fair thing to show."""


async def get_self_fqdn() -> str | None:
    """Return the local node's Tailscale FQDN, or None if unavailable.

    Returns the lowercased FQDN (matching the normalization in
    api_flock._discover_tailnet_candidates so cross-reference stays
    case-stable). None when:
    - the `tailscale` binary isn't on PATH
    - `tailscale status --json` times out / errors / returns non-JSON
    - the parsed payload has no Self.DNSName

    Deliberately does NOT consider BackendState: FqdnRedirectMiddleware
    wants the canonical name whenever tailscaled knows it. Callers that
    need "and the node is actually UP" want `get_self_fqdn_online`.

    Caches both hits AND misses for 60s so a cold-start backend with
    tailscaled not yet up doesn't shell out on every request.
    """
    fqdn, _running = await _cached_self()
    return fqdn


async def get_self_fqdn_online() -> str | None:
    """The local node's Tailscale FQDN, but ONLY while tailscaled reports
    BackendState == "Running". None otherwise.

    This is the boot card's question: qarl's rule is "show the full
    Tailscale name when Tailscale is ACTIVE, the .local name when it
    isn't". Self.DNSName survives in `tailscale status --json` output
    while the node is Stopped/NeedsLogin, so keying off the FQDN's mere
    presence would advertise a tailnet URL on a sign whose tailnet is
    down. Shares the 60s cache with `get_self_fqdn`, so adding this
    costs no extra subprocess.
    """
    fqdn, running = await _cached_self()
    return fqdn if running else None


def cached_self_fqdn_online() -> str | None:
    """The online FQDN if one is already CACHED, else None. Never queries,
    never blocks, never spawns a subprocess.

    For sync callers that cannot afford to wait. There is deliberately NO
    blocking twin of `get_self_fqdn_online`: an earlier draft of this
    change added one "for the supervisor's own thread", but the network
    supervisor has no thread — `supervisor_observe_loop` is an
    `asyncio.create_task` coroutine that calls the sync `apply_event`
    directly, so the card params are built ON the event loop and a
    ≤4s wedged-tailscaled probe would stall playback, renderer IPC and
    HTTP alongside it. Not offering the blocking call is what stops that
    reappearing; a caller who cannot await gets the cached answer or the
    honest `.local` fallback.

    Callers on the event loop should await `get_self_fqdn_online()`
    somewhere they CAN (the observe loop does, once per tick) to keep
    this warm.
    """
    now = time.monotonic()
    if now >= float(_cache["expires_at"]):
        return None
    return _cache["fqdn"] if _cache["running"] else None  # type: ignore[return-value]


async def _cached_self() -> tuple[str | None, bool]:
    now = time.monotonic()
    if now < float(_cache["expires_at"]):
        return _cache["fqdn"], bool(_cache["running"])  # type: ignore[return-value]
    fqdn, running = await asyncio.to_thread(_query_self)
    _store(fqdn, running, now)
    return fqdn, running


def _store(fqdn: str | None, running: bool, now: float) -> None:
    # `expires_at` is written LAST: readers check it first, so no reader
    # can mix `fqdn` from one query with `running` from another (each dict
    # store is atomic under the GIL). Two concurrent queries can still
    # lose-update — the slower one's result wins and stamps a TTL from its
    # own start time — which costs at most a stale-but-valid answer for
    # one TTL, never an incoherent pair.
    _cache["fqdn"] = fqdn
    _cache["running"] = running
    _cache["expires_at"] = now + _CACHE_TTL_SECONDS


def clear_cache() -> None:
    """Drop the cached FQDN so the next lookup re-queries tailscaled.

    Provided for tests that want a fresh lookup window without waiting
    out the 60s TTL. Production code should not need to call this.
    """
    _cache["fqdn"] = None
    _cache["running"] = False
    _cache["expires_at"] = 0.0


def _query_self() -> tuple[str | None, bool]:
    """(fqdn, is_running) from one `tailscale status --json`.

    Synchronous: the async path wraps this in a thread. One query answers
    both questions so the two accessors can never disagree about the same
    tailscaled, and so adding the online check costs no extra subprocess.
    """
    if not shutil.which("tailscale"):
        return None, False
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        log.warning("tailscale status probe timed out / failed to spawn")
        return None, False
    if out.returncode != 0:
        return None, False
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        log.warning("tailscale status returned non-JSON stdout")
        return None, False
    self_node = payload.get("Self")
    if not isinstance(self_node, dict):
        return None, False
    # "Reachable at our FQDN", not merely "tailscaled is alive":
    #  * BackendState Running -- the node is up (not Stopped/NeedsLogin);
    #  * a TailscaleIP assigned -- Running with no IP yet resolves to
    #    nothing, so the card would print a URL that goes nowhere;
    #  * key not Expired -- an expired node is in the netmap but refuses
    #    traffic.
    running = (
        payload.get("BackendState") == _BACKEND_STATE_RUNNING
        and bool(self_node.get("TailscaleIPs"))
        and not self_node.get("Expired")
    )
    dns_name = (self_node.get("DNSName") or "").strip().rstrip(".").lower()
    return (dns_name or None), running
