"""Pure-Python in-memory token bucket for unauth-endpoint throttling.

Sized for openMarquee's single-tenant deployment: a small dict keyed
by source IP, no DB, no external state store. Loses its state on
process restart (acceptable -- a parallelized dictionary attack
restarting on every backend restart is itself a signal worth
investigating in the journal).

Used by /api/auth/login (2026-05-25 Bundle B2 item 7) so a LAN /
tailnet attacker can't parallelize a password-guess attack against
the argon2-verified bearer-token issuance. Without this, a $50
laptop on the operator's WiFi could rate-limit-itself only at
argon2's ~250ms per attempt -- ~14400 attempts/hour, plenty for a
weak-password dictionary attack.

The defaults are conservative: 5 attempts per 60s window per source
IP. A legitimate operator-fat-fingers run stays well under that; a
script does not.

Safe-by-default: if the caller can't reliably identify the source
IP (request.client.host returns None or non-string), all such
traffic shares one global "unknown" bucket. The operator UX
degrades (multiple devices on misconfigured proxies share a bucket)
but the brute-force gate stays closed.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Simple per-key sliding-window token bucket.

    `capacity` tokens per `window_seconds`. Each successful `try_take`
    consumes a token; when empty, returns False until the window has
    passed since the OLDEST consumed token (sliding-window semantics).

    Implementation is a per-key deque of consumption timestamps; on
    each try_take we prune timestamps older than `window_seconds`
    and check if the remaining count is below capacity.

    Thread-safe via a single Lock -- the rates are low enough (one
    POST every few seconds per IP at worst-case operator-fat-fingers,
    much higher under attack) that lock contention is not a concern.

    Memory: bounded by `capacity` timestamps per active IP. A
    sustained attack from 10000 distinct IPs at full capacity =
    50000 timestamps = ~2MB Python object overhead. Acceptable on
    the Pi for any realistic threat. The dict never garbage-collects
    expired keys, so a long-running process facing scanning traffic
    would slowly grow -- the periodic _prune_empty_keys() call
    (invoked on every try_take to amortize) drops keys whose deque
    has emptied.
    """

    def __init__(self, capacity: int, window_seconds: float) -> None:
        self._capacity = capacity
        self._window = window_seconds
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def try_take(self, key: str) -> bool:
        """Attempt to consume a token for `key`. Returns True on
        success (under capacity), False if the bucket is exhausted."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            ts_list = self._buckets.get(key)
            if ts_list is None:
                ts_list = []
                self._buckets[key] = ts_list
            # Prune expired timestamps in-place.
            ts_list[:] = [t for t in ts_list if t >= cutoff]
            if len(ts_list) >= self._capacity:
                # Bucket exhausted; don't add a timestamp on failure
                # (otherwise hostile traffic could keep the window
                # rolling forward and lock out legitimate retries
                # forever).
                return False
            ts_list.append(now)
            # Opportunistic GC: if a key's deque is empty after the
            # prune (above), it gets pruned to a fresh empty list by
            # the assignment, then immediately appended-to. So empty-
            # deque cleanup happens via the explicit sweep below
            # rather than per-call.
            if len(self._buckets) > 1024:
                self._prune_empty_keys_locked()
            return True

    def reset(self, key: str | None = None) -> None:
        """Drop bucket state. With key=None drops everything (test
        affordance); with a specific key drops just that bucket
        (operator-rescue affordance for a future admin endpoint)."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def _prune_empty_keys_locked(self) -> None:
        """Called under the lock when the dict has grown past 1024
        keys. Drops keys whose deques are entirely outside the
        window (would have pruned to empty on the next try_take
        anyway). Bounds dict growth under hostile-scanning load."""
        now = time.monotonic()
        cutoff = now - self._window
        for key in list(self._buckets.keys()):
            if not any(t >= cutoff for t in self._buckets[key]):
                del self._buckets[key]


# Module-level instance for the /api/auth/login route. 5 attempts
# per 60s window per source IP is the audit-spec default; operator
# fat-fingering stays well under, attack-scripts get blocked at the
# 6th attempt within a minute.
LOGIN_BUCKET = TokenBucket(capacity=5, window_seconds=60.0)


# Sentinel key used when the caller can't identify a source IP.
# Single bucket for all-unknown traffic per the safe-default note
# in the module docstring. ALL such requests share this bucket; if
# a real attacker can flood "unknown" they'll get rate-limited too.
UNKNOWN_IP_KEY = "<unknown>"


def client_ip_or_unknown(client_host: str | None) -> str:
    """Normalize a request.client.host value into a TokenBucket key.

    Returns the host string when it looks like a usable identifier,
    or `UNKNOWN_IP_KEY` (the shared-bucket sentinel) when the value
    is None / empty / suspicious. This is where the safe-default
    behavior lives: any time we can't trust the IP, throttling
    collapses to one bucket for all such traffic.
    """
    if not isinstance(client_host, str):
        return UNKNOWN_IP_KEY
    host = client_host.strip()
    if not host:
        return UNKNOWN_IP_KEY
    return host
