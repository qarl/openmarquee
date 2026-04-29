"""REST API for the flock — the peer openMarquee devices this one
knows about.

    GET    /api/flock            — list known peers
    POST   /api/flock            — add a peer by address
    PATCH  /api/flock/{peer_id}  — toggle sync, update cached name, etc.
    DELETE /api/flock/{peer_id}  — forget a peer

    GET    /api/flock/manifest   — what this device holds right now
                                   (for peers pulling to catch up)

The manifest surface is what a pulling peer reads to decide "what do
I need to fetch / delete?". Push notifications + periodic pull arrive
in follow-up modules on top of this data.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_content_storage,
    get_flock_storage,
    get_flock_sync,
    get_tombstone_storage,
)
from openmarquee.flock import FLOCK_ADDRESS_PATTERN, Flock, FlockPeer, FlockStorage
from openmarquee.flock_sync import FlockSync, NotifyKind
from openmarquee.tombstone import Tombstone, TombstoneStorage

router = APIRouter(prefix="/api/flock", tags=["flock"])

FlockDep = Annotated[FlockStorage, Depends(get_flock_storage)]
ContentDep = Annotated[ContentStorage, Depends(get_content_storage)]
TombstoneDep = Annotated[TombstoneStorage, Depends(get_tombstone_storage)]
FlockSyncDep = Annotated[FlockSync, Depends(get_flock_sync)]

MANIFEST_SCHEMA_VERSION = 1


class ManifestEntry(BaseModel):
    """One piece of content this device currently holds."""

    content_id: UUID
    content_type: str = Field(
        description="Matches ContentItem.type — 'text', 'image', or 'video'."
    )
    updated_at: datetime


class Manifest(BaseModel):
    """What a pulling peer gets back from GET /api/flock/manifest.

    Entries + tombstones together let the peer compute a diff:
      - id in entries, not locally        → fetch it
      - id in entries, locally older      → refetch
      - id in tombstones, held locally    → delete it
      - id held locally, not on our side  → leave alone (we might catch
                                            up when they push to us)
    """

    schema_version: int = MANIFEST_SCHEMA_VERSION
    entries: list[ManifestEntry] = Field(default_factory=list)
    tombstones: list[Tombstone] = Field(default_factory=list)


class AddPeerBody(BaseModel):
    """Wire format for POST /api/flock."""

    address: str = Field(
        min_length=1,
        max_length=253,
        pattern=FLOCK_ADDRESS_PATTERN.pattern,
    )

    @field_validator("address", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        # Pasted hostnames routinely carry trailing/leading whitespace —
        # strip before pattern-matching so the operator doesn't hit a 422
        # over invisible characters.
        if isinstance(value, str):
            return value.strip()
        return value


class UpdatePeerBody(BaseModel):
    """Wire format for PATCH /api/flock/{peer_id}. Any field omitted is
    left unchanged."""

    sync: bool | None = None
    name: str | None = Field(default=None, max_length=64)


@router.get("", response_model=Flock)
async def list_flock(storage: FlockDep) -> Flock:
    return storage.load()


@router.get("/manifest", response_model=Manifest)
async def get_manifest(
    content: ContentDep, tombstones: TombstoneDep
) -> Manifest:
    """Inventory this device exposes to pulling peers.

    Entries are live content_ids + their updated_at (for last-writer-wins
    comparisons). Tombstones are recently-deleted ids so a peer can catch
    up on deletions it missed while offline.
    """
    entries = [
        ManifestEntry(
            content_id=item.id,
            content_type=item.type,
            updated_at=content.read_updated_at(item.id),
        )
        for item in content.list_all()
    ]
    return Manifest(entries=entries, tombstones=tombstones.list_active())


@router.post("", response_model=FlockPeer, status_code=201)
async def add_peer(
    body: AddPeerBody,
    storage: FlockDep,
    sync: FlockSyncDep,
    background: BackgroundTasks,
) -> FlockPeer:
    try:
        peer = storage.add(address=body.address)
    except ValueError as exc:
        # Duplicate address — 409 Conflict reads better than 400 here.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Fire-and-forget probe of the new peer's /api/settings so the tile's
    # display name switches from the raw address to the configured
    # sign_name within a second or two of the add.
    background.add_task(sync.probe_peer_name, peer.address)
    # SYSTEM_SPEC §13 introduction protocol: hello-ping the new peer
    # (so it adds us back) AND notify our existing flock peers (so
    # they add the new peer too). After settling, full-mesh peer
    # awareness — operator only has to "Add Peer" on one device.
    # Loop prevention lives in the receiver: /api/flock/hello does
    # NOT cascade further. Best-effort fan-out, errors logged.
    background.add_task(sync.gossip_add, peer.address)
    return peer


@router.patch("/{peer_id}", response_model=FlockPeer)
async def update_peer(
    peer_id: UUID,
    body: UpdatePeerBody,
    storage: FlockDep,
    sync: FlockSyncDep,
    background: BackgroundTasks,
) -> FlockPeer:
    peer = storage.update(peer_id, sync=body.sync, name=body.name)
    if peer is None:
        raise HTTPException(status_code=404, detail=f"no peer {str(peer_id)!r}")
    # Mirror a sync-flag flip back to the peer so their UI reflects the
    # change without waiting for the next pull-worker tick.
    if body.sync is not None:
        background.add_task(
            sync.announce_sync_to_peer, peer.address, bool(body.sync)
        )
    return peer


@router.delete("/{peer_id}", status_code=204)
async def delete_peer(peer_id: UUID, storage: FlockDep) -> None:
    if not storage.remove(peer_id):
        raise HTTPException(status_code=404, detail=f"no peer {str(peer_id)!r}")


class NotifyBody(BaseModel):
    """Wire format for POST /api/flock/notify. A peer is telling us a piece
    of content changed on their end; we'll pull it back from `sender_address`
    using the standard /api/content/* surface. `at` stamps the sender's
    wall-clock moment of the change — used to skip stale deletes that
    race a local edit."""

    content_id: UUID
    kind: NotifyKind
    sender_address: str = Field(
        min_length=1,
        max_length=253,
        pattern=FLOCK_ADDRESS_PATTERN.pattern,
    )
    at: datetime

    @field_validator("sender_address", mode="before")
    @classmethod
    def _normalize_sender(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class HelloBody(BaseModel):
    """Wire format for POST /api/flock/hello — gossip-on-add (§13).

    `address` is the introduced peer's tailnet hostname / IPv4. The
    receiver adds it to its own flock if not already present and does
    NOT cascade further (loop prevention). Used both for the
    reciprocal-add hello (A→B carries A's address) and the
    forward-notification hello (A→C carries the new B's address)."""

    address: str = Field(
        min_length=1,
        max_length=253,
        pattern=FLOCK_ADDRESS_PATTERN.pattern,
    )

    @field_validator("address", mode="before")
    @classmethod
    def _normalize_address(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class SyncAnnounceBody(BaseModel):
    """Wire format for POST /api/flock/sync-announce — a peer is telling
    us they flipped their sync flag for us, and we should mirror it."""

    sender_address: str = Field(
        min_length=1,
        max_length=253,
        pattern=FLOCK_ADDRESS_PATTERN.pattern,
    )
    sync: bool

    @field_validator("sender_address", mode="before")
    @classmethod
    def _normalize_sender(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


@router.post("/hello", status_code=204)
async def receive_hello(body: HelloBody, sync: FlockSyncDep) -> None:
    """SYSTEM_SPEC §13 introduction protocol: a peer is telling us
    about a flock member (either themselves, in the reciprocal-add
    case, or another peer in the forward-notification case). We add
    the address to our flock if not already present and DO NOT
    cascade further — gossip_add fans out only on operator-driven
    POST /api/flock, never on inbound hellos. Idempotent: duplicate
    hellos for the same address are 204 no-ops, since gossip races
    can introduce the same peer twice (once via reciprocal, once via
    forward).

    Unlike /notify and /sync-announce, /hello accepts addresses we
    don't yet know about — that's the entire point of an
    introduction protocol. The address-format validator on HelloBody
    blocks the SSRF-shape concerns."""
    sync.apply_hello(body.address)


@router.post("/sync-announce", status_code=204)
async def receive_sync_announce(
    body: SyncAnnounceBody, sync: FlockSyncDep
) -> None:
    """A peer flipped their sync flag for us — mirror it on our side so
    both UIs agree without waiting for a pull round. Only accepted from
    addresses that are already in our flock (same allowlist model as
    /notify)."""
    ok = sync.apply_sync_announcement(body.sender_address, body.sync)
    if not ok:
        raise HTTPException(
            status_code=403,
            detail=f"peer {body.sender_address!r} is not in this device's flock",
        )


@router.post("/notify", status_code=204)
async def receive_notify(
    body: NotifyBody, sync: FlockSyncDep, flock: FlockDep
) -> None:
    """Inbound push from a flock peer. For 'updated' we pull the content
    back from the sender and save it locally (skipping if our copy is
    newer). For 'deleted' we record a tombstone + drop our copy.

    Only accept pushes from addresses we've explicitly added to our own
    flock — prevents a tailnet node we don't sync with from seeding
    arbitrary content into our store via the /api/content/* pull.
    """
    if flock.load().find_by_address(body.sender_address) is None:
        raise HTTPException(
            status_code=403,
            detail=f"peer {body.sender_address!r} is not in this device's flock",
        )
    await sync.ingest_push(body.content_id, body.kind, body.sender_address, body.at)
