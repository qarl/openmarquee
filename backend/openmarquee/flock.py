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

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

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


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_address(address: str) -> str:
    # DNS names are case-insensitive; lowercase on entry so dedup works
    # regardless of how the operator typed the Tailscale hostname.
    return address.strip().lower()


class FlockPeer(BaseModel):
    """One remote openMarquee device in this device's flock."""

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
        "(e.g. 'hub75-128x64', 'hdmi-1080', 'ws2812-strip'). Used "
        "by the flock UI's stats grid for the aspect-ratio label.",
    )
    signal: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Peer's WiFi RSSI as a percentage (0-100). "
        "Populated by Phase B health probes.",
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
    _stats: dict[str, int] = {"load_calls": 0, "save_calls": 0}

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def stats_snapshot(cls) -> dict[str, int]:
        return dict(cls._stats)

    def load(self) -> Flock:
        type(self)._stats["load_calls"] += 1
        if not self.path.exists():
            return Flock()
        data = json.loads(self.path.read_text())
        return Flock.model_validate(data)

    def save(self, flock: Flock) -> None:
        type(self)._stats["save_calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(flock.model_dump_json(indent=2))
        tmp.replace(self.path)

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
