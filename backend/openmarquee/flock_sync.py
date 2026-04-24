"""Flock sync engine — push local changes to peers; ingest peer pushes.

Push-on-change drives latency (a sign edit shows up on peers within
seconds). A periodic pull worker (Phase 4) provides the reliability
backstop for peers that missed a push — offline, network glitch, or
a push that raced with a local delete.

Push is notify-only: the sender POSTs a tiny {content_id, kind,
sender_address} to the receiver's /api/flock/notify, who pulls the
actual content back using the standard /api/content/* endpoints. Two
wins: push payloads stay small, and the same HTTP surface the browser
uses serves peers — no parallel binary protocol.

Loop prevention: the notify hook is at the HTTP-layer (route handlers
in api.py), not inside the storage layer. When we ingest a peer's
push we call ContentStorage.save() directly, which doesn't fire a
notify — so A→B→A→... can't spiral.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from openmarquee.content import ContentItem, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.flock import FlockStorage
from openmarquee.tombstone import TombstoneStorage

logger = logging.getLogger(__name__)

NotifyKind = Literal["updated", "deleted"]

_CONTENT_ADAPTER: TypeAdapter[ContentItem] = TypeAdapter(ContentItem)


class FlockSync:
    """Orchestrates push-on-change to sync=True peers + ingest of peer pushes.

    HTTP client construction is factored behind `http_client_factory` so
    tests can swap in a MockTransport without the code under test knowing.
    """

    def __init__(
        self,
        *,
        content_storage: ContentStorage,
        tombstone_storage: TombstoneStorage,
        flock_storage: FlockStorage,
        get_self_address: Callable[[], str | None],
        timeout_seconds: float = 5.0,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.content = content_storage
        self.tombstones = tombstone_storage
        self.flock = flock_storage
        self._get_self_address = get_self_address
        self.timeout = timeout_seconds
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_seconds)
        )

    # --- outbound push ---

    async def notify_peers(self, content_id: UUID, kind: NotifyKind) -> None:
        """Fire-and-forget push to every sync=True peer. Failures logged,
        never raised — the pull worker recovers dropped pushes."""
        peers = [p for p in self.flock.load().peers if p.sync]
        if not peers:
            return
        sender = self._get_self_address()
        if sender is None:
            logger.warning(
                "skipping push %s/%s: device has no reachable self-address "
                "(set SystemSettings.tailscale_hostname)",
                content_id,
                kind,
            )
            return
        # Stamp delete pushes with the wall-clock moment of the delete so a
        # peer that's concurrently editing the same id can skip a stale
        # delete. `updated` pushes carry a hint too, but the authoritative
        # updated_at comes from the sender's manifest during pull.
        at = datetime.now(timezone.utc)
        async with self._client_factory() as client:
            await asyncio.gather(
                *(
                    self._push_one(client, p.address, content_id, kind, sender, at)
                    for p in peers
                ),
                return_exceptions=True,
            )

    async def _push_one(
        self,
        client: httpx.AsyncClient,
        peer_address: str,
        content_id: UUID,
        kind: NotifyKind,
        sender_address: str,
        at: datetime,
    ) -> None:
        url = f"http://{peer_address}/api/flock/notify"
        payload = {
            "content_id": str(content_id),
            "kind": kind,
            "sender_address": sender_address,
            "at": at.isoformat(),
        }
        try:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logger.warning(
                    "push %s/%s -> %s returned HTTP %d",
                    content_id,
                    kind,
                    peer_address,
                    r.status_code,
                )
        except Exception:
            logger.exception("push %s/%s -> %s failed", content_id, kind, peer_address)

    # --- inbound push ingestion ---

    async def ingest_push(
        self,
        content_id: UUID,
        kind: NotifyKind,
        sender_address: str,
        at: datetime,
    ) -> None:
        """Apply a push received from a peer. Safe to call concurrently for
        distinct content_ids; behavior under racing pushes of the same id
        is last-writer-wins by sender-stamped timestamps."""
        if kind == "deleted":
            self._ingest_delete(content_id, at)
        elif kind == "updated":
            await self._ingest_update(content_id, sender_address)
        else:
            logger.warning("unknown notify kind %r for %s", kind, content_id)

    def _ingest_delete(self, content_id: UUID, deleted_at: datetime) -> None:
        # LWW vs a concurrent local edit: if we hold a copy stamped AFTER
        # the delete, the edit won the race and we keep it. Skipping the
        # tombstone too — a tombstone would just re-propagate the stale
        # delete on the next pull.
        if self.content.exists(content_id):
            local_ts = self.content.read_updated_at(content_id)
            if local_ts > deleted_at:
                logger.info(
                    "skipping stale delete %s (local %s > sender %s)",
                    content_id,
                    local_ts,
                    deleted_at,
                )
                return
            self.tombstones.add(content_id, now=deleted_at)
            self.content.delete(content_id)
        else:
            self.tombstones.add(content_id, now=deleted_at)

    async def _ingest_update(self, content_id: UUID, sender_address: str) -> None:
        async with self._client_factory() as client:
            manifest_r = await client.get(
                f"http://{sender_address}/api/flock/manifest"
            )
            manifest_r.raise_for_status()
            entry = next(
                (
                    e
                    for e in manifest_r.json()["entries"]
                    if e["content_id"] == str(content_id)
                ),
                None,
            )
            if entry is None:
                # Sender evicted between push + our fetch, or pushed an id
                # it doesn't actually hold. No-op — pull worker picks it up
                # if it reappears.
                logger.info(
                    "peer %s pushed %s but manifest doesn't list it; skipping",
                    sender_address,
                    content_id,
                )
                return
            sender_updated_at = datetime.fromisoformat(entry["updated_at"])

            # Last-writer-wins. A stale push from before our edit shouldn't
            # clobber the local edit.
            if self.content.exists(content_id):
                local_ts = self.content.read_updated_at(content_id)
                if local_ts >= sender_updated_at:
                    return

            item_r = await client.get(
                f"http://{sender_address}/api/content/{content_id}"
            )
            item_r.raise_for_status()
            item = _CONTENT_ADAPTER.validate_python(item_r.json())

            asset_r = await client.get(
                f"http://{sender_address}/api/content/{content_id}/asset"
            )
            asset_r.raise_for_status()
            asset_bytes = asset_r.content

            if isinstance(item, VideoSlide):
                video_r = await client.get(
                    f"http://{sender_address}/api/content/{content_id}/video"
                )
                video_r.raise_for_status()
                self.content.save_video(
                    item,
                    asset_bytes,
                    video_r.content,
                    updated_at=sender_updated_at,
                )
            else:
                self.content.save(
                    item, asset_bytes, updated_at=sender_updated_at
                )
