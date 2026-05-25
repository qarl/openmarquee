"""Flock — a set of peer openMarquee devices this one knows about.
Optionally kept in media-sync with a subset of them (sync=True).

This module is the storage + data model. Peer-to-peer networking
(manifest endpoints, push notifications, periodic pull) ships in
follow-up commits; Phase 1 is local CRUD only so the UI has a
stable API surface to wire against.

Storage shape (flock.json):

    {
      "schema_version": 1,
      "peers": [
        {
          "id": "<local uuid>",
          "address": "signs-lobby.tailnet-xyz.ts.net",
          "name": null,            // populated from a reachability probe
          "sync": false,
          "added_at": "...",
          "last_seen_at": null
        },
        ...
      ]
    }
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openmarquee._atomic import atomic_write_text
from openmarquee._storage_recovery import quarantine_corrupt_file

FLOCK_SCHEMA_VERSION = 1

# A peer address must look like a DNS hostname or a bare IPv4 literal,
# optionally followed by `:port`. Schemes, paths, and arbitrary port
# strings are rejected — the sync layer feeds this straight into an
# HTTP client, so anything weirder is an SSRF-shaped foot-gun.
# IPv6 literals aren't supported yet — Tailscale magic-DNS + IPv4 is
# what the real use cases look like.
FLOCK_ADDRESS_PATTERN = re.compile(
    r"^[A-Za-z0-9]"
    r"(?:[A-Za-z0-9\-\.]{0,251}[A-Za-z0-9])?"
    r"(?::[0-9]{1,5})?$"
)

# Tailscale magic-DNS suffix regex: <label>(.label)*.ts.net where
# each label is DNS-clean (alphanumeric + hyphen, no embedded IPs or
# bare dots). Without this, a pure `endswith(".ts.net")` check would
# accept e.g. `1.2.3.4.ts.net` or `foo..bar.ts.net` — Tailscale's
# DNS won't resolve them, but a misconfigured operator resolver
# could forward unknown `*.ts.net` queries elsewhere and weaken the
# "registry-controlled TLD" trust assumption.
_TAILSCALE_MAGIC_DNS_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9][a-z0-9\-]*)*\.ts\.net$"
)

# Trusted-peer IPv4 ranges for is_trusted_peer_address(). Explicit
# rather than relying on `ipaddress.IPv4Address.is_private` because
# the latter is over-broad: it returns True for RFC5737 documentation
# ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) and other non-
# globally-reachable blocks that aren't actual trusted-peer addresses
# in the openMarquee deployment model. Mirroring the constants block
# here keeps the trust surface explicit + auditable.
_TRUSTED_PEER_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918 class A
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918 class B
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918 class C
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("100.64.0.0/10"),  # Tailscale CGNAT
)


def is_trusted_peer_address(addr: str) -> bool:
    """True iff `addr` is an IP / hostname that the device should
    accept as a flock-peer candidate without operator confirmation.

    Used as a defense-in-depth gate on the UNAUTHENTICATED POST
    /api/flock/hello: without it, an attacker on the LAN can post a
    public IP or any internal-LAN host, then the backend stores it +
    fires a background GET against the address (blind SSRF / portscan
    primitive). Per the 2026-05-24 security audit MEDIUM finding 2.

    Accepted shapes:
    - RFC1918 IPv4 (10.x, 172.16-31.x, 192.168.x) -- same LAN as Pi
    - Loopback IPv4 (127.x) -- local development
    - Tailscale CGNAT IPv4 (100.64.0.0/10) -- tailnet-assigned addr
    - `*.ts.net` hostname -- Tailscale magic-DNS shape (registry-
      controlled TLD; an attacker can't register a non-Tailscale
      `*.ts.net` without compromising Tailscale's registry)

    The `:port` suffix on `addr` is stripped before the check (the
    FLOCK_ADDRESS_PATTERN regex permits it; the trust check operates
    on the host alone).

    Operator-driven POST /api/flock (the authenticated path) does
    NOT use this gate -- the operator's intent IS the trust signal
    there. Only /api/flock/hello (unauth gossip-on-add) needs the
    SSRF-shape protection.
    """
    # Strip optional :port suffix before evaluating the host.
    host = addr.rsplit(":", 1)[0] if ":" in addr and addr.count(":") == 1 else addr
    # Tailscale magic-DNS subdomain shape: must be DNS-label-clean
    # under the .ts.net suffix. Pure `endswith(".ts.net")` would
    # accept embedded IPs / double-dots; the regex enforces real
    # subdomain shape.
    if _TAILSCALE_MAGIC_DNS_PATTERN.match(host):
        # Belt-and-suspenders: the regex accepts digit-only labels
        # (legitimate DNS), so an attacker could still smuggle an
        # IPv4 literal as the subdomain (e.g. `1.2.3.4.ts.net`).
        # Reject if the prefix-before-.ts.net parses as an IP.
        prefix = host[: -len(".ts.net")]
        try:
            ipaddress.ip_address(prefix)
            return False
        except ValueError:
            pass
        return True
    # IP literal: must be in one of the explicit trusted ranges.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP and not *.ts.net -> reject. This blocks public-
        # hostname SSRF targets (evil.com, attacker.example) and any
        # other non-Tailscale DNS name an attacker might supply.
        return False
    if not isinstance(ip, ipaddress.IPv4Address):
        # IPv6 is not yet supported in the flock-address surface
        # (FLOCK_ADDRESS_PATTERN's docstring also notes this).
        return False
    return any(ip in net for net in _TRUSTED_PEER_RANGES)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_address(address: str) -> str:
    # DNS names are case-insensitive; lowercase on entry so dedup works
    # regardless of how the operator typed the Tailscale hostname.
    return address.strip().lower()


class FlockPeer(BaseModel):
    """One remote openMarquee device in this device's flock."""

    # Round-13 forward-compat fix (2026-05-25): preserve unknown
    # per-peer fields across the FlockStorage round-trip. Pre-fix,
    # Flock.model_validate ran under default extra="ignore", so any
    # forward-compat per-peer field (e.g. a future capabilities list,
    # or a new health-probe stat from backend N+1) was silently
    # dropped on every load. add/remove/update then save() rewrote
    # the lossy peer list -- a single click on remove-peer would
    # strip the field from EVERY OTHER peer too.
    #
    # In Pydantic v2 with extra="allow", model_dump_json (used by
    # FlockStorage.save at flock.py:337) emits extras alongside known
    # fields. No save-handler changes needed.
    #
    # Same shape as the SystemSettings fix (r12) -- model IS the
    # on-disk shape, no envelope-vs-inner split.
    model_config = ConfigDict(extra="allow")

    id: UUID = Field(default_factory=uuid4)
    address: str = Field(
        min_length=1,
        max_length=253,
        description="Tailscale magic-DNS name or bare IPv4 — whatever reaches the peer.",
    )

    @field_validator("address", mode="before")
    @classmethod
    def _check_address(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("address must be a string")
        normalized = _normalize_address(value)
        if not FLOCK_ADDRESS_PATTERN.match(normalized):
            raise ValueError(
                f"address {value!r}: expected DNS hostname or IPv4 literal, no scheme/port/path"
            )
        return normalized

    name: str | None = Field(
        default=None,
        max_length=64,
        description="Peer's reported sign_name, filled in once we've probed it. "
        "Stays None until the first successful reachability check.",
    )
    sync: bool = Field(
        default=False,
        description="If true, keep this device's media in sync with the peer "
        "(push on local change + periodic pull). If false, the "
        "peer is tracked but its media stays independent.",
    )
    added_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime | None = Field(
        default=None,
        description="UTC of the last successful reachability probe. None means "
        "we've never reached this peer (just-added or offline).",
    )

    # Health-probe fields surfaced in the design's flock-grid card stats
    # row (model / mode / signal / uptime). All optional and default to
    # None — Phase A leaves the probe wiring as future work, so the UI
    # has to render gracefully against missing data. Phase B adds the
    # per-peer /api/system/info probe + populates these. Schema version
    # stays at 1 because these are new optional fields (per SYSTEM_SPEC
    # §3.3.2 convention — version bumps only on non-backward-compatible
    # changes).
    model: str | None = Field(
        default=None,
        max_length=64,
        description="Peer's hardware identifier (e.g. 'Pi Zero 2 W', 'Pi 4'). "
        "Populated by Phase B health probes.",
    )
    mode: str | None = Field(
        default=None,
        max_length=32,
        description="Peer's output mode + display dims as a slug "
        "(e.g. 'hdmi-1080'). Used by the flock UI's stats grid for "
        "the aspect-ratio label.",
    )
    signal: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Peer's WiFi RSSI as a percentage (0-100). Populated by Phase B health probes.",
    )
    uptime: str | None = Field(
        default=None,
        max_length=32,
        description="Pre-formatted uptime string (e.g. '4d 7h'). "
        "Computed peer-side and reported via Phase B's health probe; "
        "stored as a string rather than seconds because the formatting "
        "is a UI concern and the operator never does math on it.",
    )

    # Phase B.3 — out-of-sync diff. Count of content items the peer
    # has that we don't (so we're "behind by N" relative to them).
    # Computed during pull_from_peer pre-apply + stored back on the
    # peer record; UI reads it at render time without a cross-device
    # probe. None means "never pulled" or "pull-on-this-peer is off
    # (sync=False)" — UI surfaces both as "—" rather than 0.
    #
    # TODO(qarl-confirm): the "we're behind them" semantic is the
    # default; an alternative ("they're behind us") would require a
    # second field. Operator's read of "this peer is N items
    # behind" is more naturally "we're missing N of their items"
    # since the operator sees the peer card from THIS device's
    # perspective. Flip if operator-mental-model says otherwise.
    #
    # TODO(qarl-confirm): for sync=False peers we leave items_behind
    # at None. An alternative ("compute it anyway as a what-if-I-
    # synced preview") would require running the manifest fetch
    # outside the pull worker — wasteful when the operator hasn't
    # opted into sync. Flip if the preview is wanted.
    #
    # TODO(qarl-confirm): when an operator flips sync=True -> False
    # mid-flock, the previously-stored items_behind value persists
    # until the next pull (which won't happen since sync is off).
    # Default leaves it stale — the UI can gate display on the
    # current sync flag and read items_behind only when sync==True.
    # Alternative: clear items_behind to None on sync=True->False
    # transitions in PATCH /api/flock/{peer}. Flip if you'd rather
    # the data stay strictly fresh-or-absent.
    items_behind: int | None = Field(
        default=None,
        ge=0,
        description="Count of items the peer has that we don't, as of "
        "the most recent successful pull. None when never pulled or "
        "when sync=False on this peer.",
    )


class Flock(BaseModel):
    """Envelope wrapping the peer list + schema version for on-disk storage."""

    # Round-13 forward-compat fix: preserve unknown ENVELOPE-LEVEL
    # fields (e.g. a future top-level stat or feature flag stored
    # alongside the peer list) across the load->mutate->save trio.
    # Same rationale as FlockPeer above; see also r11 ContentItem +
    # r12 SystemSettings.
    model_config = ConfigDict(extra="allow")

    schema_version: int = FLOCK_SCHEMA_VERSION
    peers: list[FlockPeer] = Field(default_factory=list)

    def find(self, peer_id: UUID) -> FlockPeer | None:
        for p in self.peers:
            if p.id == peer_id:
                return p
        return None

    def find_by_address(self, address: str) -> FlockPeer | None:
        """Dedupe lookup — address is the user-visible uniqueness key (you
        shouldn't add the same Tailscale host twice). Case-insensitive to
        match DNS semantics."""
        needle = _normalize_address(address)
        for p in self.peers:
            if p.address == needle:
                return p
        return None


class FlockStorage:
    """Atomic file-backed persistence for the flock."""

    # Perf counters (Batch 6.1). See ContentStorage._stats comment.
    # json_parses (Batch 7.2) separates true re-parses from cache hits.
    _stats: dict[str, int] = {
        "load_calls": 0,
        "save_calls": 0,
        "json_parses": 0,
    }

    def __init__(self, path: Path):
        self.path = Path(path)
        # mtime-keyed cache (Batch 7.2). See PlaylistStorage._cache.
        self._cache: tuple[int, Flock] | None = None

    @classmethod
    def stats_snapshot(cls) -> dict[str, int]:
        return dict(cls._stats)

    def load(self) -> Flock:
        type(self)._stats["load_calls"] += 1
        if not self.path.exists():
            return Flock()
        mtime_ns = self.path.stat().st_mtime_ns
        if self._cache is not None and self._cache[0] == mtime_ns:
            return self._cache[1]
        type(self)._stats["json_parses"] += 1
        # 19.2 / sweep #10 #4: see PlaylistStorage.load_all note.
        try:
            data = json.loads(self.path.read_text())
            flock = Flock.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            quarantine_corrupt_file(self.path, exc)
            return Flock()
        self._cache = (mtime_ns, flock)
        return flock

    def save(self, flock: Flock) -> None:
        type(self)._stats["save_calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # See PlaylistStorage.save_all comment -- clear cache BEFORE
        # the write so a failed atomic replace doesn't leave the
        # cache holding a caller-mutated object that doesn't match
        # disk. add()/remove()/update() mutate the load() return
        # in-place before calling save.
        self._cache = None
        # 11.2: atomic_write_text sets 0600 + cleans up orphan .tmp.
        atomic_write_text(self.path, flock.model_dump_json(indent=2))

    # --- convenience CRUD on top of load/save ---

    def add(self, address: str) -> FlockPeer:
        """Add a peer by address. Raises ValueError if the address is
        already present (addresses are the uniqueness key)."""
        flock = self.load()
        if flock.find_by_address(address) is not None:
            raise ValueError(f"peer with address {address!r} already in flock")
        peer = FlockPeer(address=address)
        flock.peers.append(peer)
        self.save(flock)
        return peer

    def remove(self, peer_id: UUID) -> bool:
        """Forget a peer. Returns True if removed, False if absent."""
        flock = self.load()
        before = len(flock.peers)
        flock.peers = [p for p in flock.peers if p.id != peer_id]
        if len(flock.peers) == before:
            return False
        self.save(flock)
        return True

    def update(
        self,
        peer_id: UUID,
        *,
        sync: bool | None = None,
        name: str | None = None,
        mark_seen: bool = False,
        items_behind: int | None = -1,
    ) -> FlockPeer | None:
        """Mutate a peer's non-id fields. Returns the updated peer, or None
        if no peer matched `peer_id`. `mark_seen=True` stamps last_seen_at.

        `items_behind` uses sentinel -1 to mean "leave unchanged" (since
        None is a meaningful value — "never pulled / sync off"). Pass an
        int to set, None to explicitly clear.
        """
        flock = self.load()
        peer = flock.find(peer_id)
        if peer is None:
            return None
        if sync is not None:
            peer.sync = sync
        if name is not None:
            peer.name = name
        if mark_seen:
            peer.last_seen_at = _now()
        if items_behind != -1:
            peer.items_behind = items_behind
        self.save(flock)
        return peer
