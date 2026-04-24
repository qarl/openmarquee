"""REST API for the flock — the peer openMarquee devices this one
knows about.

    GET    /api/flock            — list known peers
    POST   /api/flock            — add a peer by address
    PATCH  /api/flock/{peer_id}  — toggle sync, update cached name, etc.
    DELETE /api/flock/{peer_id}  — forget a peer

Networking (manifest exchange, push notifications, periodic pull) lives
in follow-up modules — this surface is just local CRUD so the UI can
render the Flock panel and the sync plumbing has somewhere to hook in.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from openmarquee.dependencies import get_flock_storage
from openmarquee.flock import FLOCK_ADDRESS_PATTERN, Flock, FlockPeer, FlockStorage

router = APIRouter(prefix="/api/flock", tags=["flock"])

FlockDep = Annotated[FlockStorage, Depends(get_flock_storage)]


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


@router.post("", response_model=FlockPeer, status_code=201)
async def add_peer(body: AddPeerBody, storage: FlockDep) -> FlockPeer:
    try:
        return storage.add(address=body.address)
    except ValueError as exc:
        # Duplicate address — 409 Conflict reads better than 400 here.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{peer_id}", response_model=FlockPeer)
async def update_peer(
    peer_id: UUID, body: UpdatePeerBody, storage: FlockDep
) -> FlockPeer:
    peer = storage.update(peer_id, sync=body.sync, name=body.name)
    if peer is None:
        raise HTTPException(status_code=404, detail=f"no peer {str(peer_id)!r}")
    return peer


@router.delete("/{peer_id}", status_code=204)
async def delete_peer(peer_id: UUID, storage: FlockDep) -> None:
    if not storage.remove(peer_id):
        raise HTTPException(status_code=404, detail=f"no peer {str(peer_id)!r}")
