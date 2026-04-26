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
from contextlib import suppress
from datetime import UTC, datetime
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
        get_sync_enabled: Callable[[], bool] | None = None,
        timeout_seconds: float = 5.0,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.content = content_storage
        self.tombstones = tombstone_storage
        self.flock = flock_storage
        self._get_self_address = get_self_address
        # Global kill switch (SystemSettings.flock_sync_enabled). When
        # False: no outbound pushes, no ingest of inbound notifies, no
        # pull-worker fetches. Defaults to always-on so tests don't
        # have to wire it explicitly.
        self._get_sync_enabled = get_sync_enabled or (lambda: True)
        self.timeout = timeout_seconds
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_seconds)
        )

    # --- outbound push ---

    async def notify_peers(self, content_id: UUID, kind: NotifyKind) -> None:
        """Fire-and-forget push to every sync=True peer. Failures logged,
        never raised — the pull worker recovers dropped pushes."""
        if not self._get_sync_enabled():
            return
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
        at = datetime.now(UTC)
        async with self._client_factory() as client:
            await asyncio.gather(
                *(self._push_one(client, p.address, content_id, kind, sender, at) for p in peers),
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
        is last-writer-wins by sender-stamped timestamps. Silently drops
        pushes when the global flock_sync_enabled kill switch is off."""
        if not self._get_sync_enabled():
            return
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
            manifest_r = await client.get(f"http://{sender_address}/api/flock/manifest")
            manifest_r.raise_for_status()
            entry = next(
                (e for e in manifest_r.json()["entries"] if e["content_id"] == str(content_id)),
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
            await self._fetch_and_save(client, sender_address, content_id, sender_updated_at)

    async def _fetch_and_save(
        self,
        client: httpx.AsyncClient,
        sender_address: str,
        content_id: UUID,
        sender_updated_at: datetime,
    ) -> None:
        """Pull one content id from a peer and save locally — shared by
        push-ingest (_ingest_update) and the pull worker (pull_from_peer).

        Skips when local is equal-or-newer (last-writer-wins)."""
        if self.content.exists(content_id):
            local_ts = self.content.read_updated_at(content_id)
            if local_ts >= sender_updated_at:
                return

        item_r = await client.get(f"http://{sender_address}/api/content/{content_id}")
        item_r.raise_for_status()
        item = _CONTENT_ADAPTER.validate_python(item_r.json())

        asset_r = await client.get(f"http://{sender_address}/api/content/{content_id}/asset")
        asset_r.raise_for_status()
        asset_bytes = asset_r.content

        if isinstance(item, VideoSlide):
            video_r = await client.get(f"http://{sender_address}/api/content/{content_id}/video")
            video_r.raise_for_status()
            self.content.save_video(
                item,
                asset_bytes,
                video_r.content,
                updated_at=sender_updated_at,
            )
        else:
            self.content.save(item, asset_bytes, updated_at=sender_updated_at)

    # --- sync-flag propagation (symmetric UX) ---

    async def announce_sync_to_peer(self, peer_address: str, sync: bool) -> None:
        """Tell `peer_address` that THIS device just flipped its sync flag
        for them. The peer updates its matching entry so the relationship
        stays visually symmetric across both UIs without waiting for the
        next pull-worker tick. Best-effort: errors are logged, not raised."""
        if not self._get_sync_enabled():
            return
        sender = self._get_self_address()
        if sender is None:
            logger.info(
                "skipping sync-announce to %s: no reachable self-address yet",
                peer_address,
            )
            return
        url = f"http://{peer_address}/api/flock/sync-announce"
        payload = {"sender_address": sender, "sync": bool(sync)}
        try:
            async with self._client_factory() as client:
                r = await client.post(url, json=payload)
                if r.status_code >= 400:
                    logger.warning(
                        "sync-announce → %s returned HTTP %d",
                        peer_address,
                        r.status_code,
                    )
        except Exception:
            logger.info("sync-announce to %s failed", peer_address)

    def apply_sync_announcement(self, sender_address: str, sync: bool) -> bool:
        """Apply an inbound sync-announce: flip our local flock entry for
        `sender_address` to `sync`. Returns True if a matching peer was
        found, False otherwise (so the HTTP route can respond 403)."""
        flock = self.flock.load()
        peer = flock.find_by_address(sender_address)
        if peer is None:
            return False
        self.flock.update(peer.id, sync=bool(sync))
        return True

    # --- peer-name probe (fills FlockPeer.name from remote's sign_name) ---

    async def probe_peer_name(self, address: str) -> None:
        """GET http://address/api/settings and, if it returns a sign_name,
        write it onto the matching FlockPeer in our flock.json. Best-effort:
        any exception is logged and swallowed — we'll try again on the
        next pull round."""
        flock = self.flock.load()
        peer = flock.find_by_address(address)
        if peer is None:
            return
        try:
            async with self._client_factory() as client:
                r = await client.get(f"http://{address}/api/settings")
                r.raise_for_status()
                remote_name = (r.json() or {}).get("sign_name")
        except Exception:
            logger.info("probe %s: settings fetch failed", address)
            return
        if not remote_name or peer.name == remote_name:
            return
        self.flock.update(peer.id, name=remote_name)

    # --- periodic pull (reliability backstop) ---

    async def pull_from_peer(self, peer_address: str) -> None:
        """One reconcile round against one peer: refresh the cached
        sign_name, fetch manifest, pull any missing/newer content, apply
        any tombstones we missed. Errors are logged, not raised — the
        loop keeps running. No-op when the global flock_sync_enabled
        kill switch is off."""
        if not self._get_sync_enabled():
            return
        await self.probe_peer_name(peer_address)
        async with self._client_factory() as client:
            try:
                manifest_r = await client.get(f"http://{peer_address}/api/flock/manifest")
                manifest_r.raise_for_status()
            except Exception:
                logger.exception("pull %s: manifest fetch failed", peer_address)
                return
            manifest = manifest_r.json()

            for entry in manifest.get("entries", []):
                cid = UUID(entry["content_id"])
                sender_ts = datetime.fromisoformat(entry["updated_at"])
                try:
                    await self._fetch_and_save(client, peer_address, cid, sender_ts)
                except Exception:
                    logger.exception("pull %s: fetch %s failed", peer_address, cid)

            for stone in manifest.get("tombstones", []):
                cid = UUID(stone["content_id"])
                deleted_at = datetime.fromisoformat(stone["deleted_at"])
                self._apply_pulled_tombstone(cid, deleted_at)

    def _apply_pulled_tombstone(self, content_id: UUID, deleted_at: datetime) -> None:
        # Same LWW rule as _ingest_delete, but via pull. Skip when a newer
        # local edit exists; otherwise record tombstone + drop the copy.
        if self.content.exists(content_id):
            local_ts = self.content.read_updated_at(content_id)
            if local_ts > deleted_at:
                return
            self.tombstones.add(content_id, now=deleted_at)
            self.content.delete(content_id)
        else:
            # Only record if we don't already have a fresher tombstone —
            # avoids flapping the on-disk log on every pull round.
            existing = next(
                (t for t in self.tombstones.load().tombstones if t.content_id == content_id),
                None,
            )
            if existing is None or existing.deleted_at < deleted_at:
                self.tombstones.add(content_id, now=deleted_at)


class PullWorker:
    """Fires FlockSync.pull_from_peer against every sync=True peer on a
    timer. Provides the reliability backstop for pushes dropped on the
    floor (peer offline, power cycle, process crash between save+push)."""

    def __init__(
        self,
        sync: FlockSync,
        interval_seconds: float = 60.0,
    ):
        self.sync = sync
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                peers = [p for p in self.sync.flock.load().peers if p.sync]
                for peer in peers:
                    await self.sync.pull_from_peer(peer.address)
                self.sync.tombstones.prune_expired()
            except Exception:
                logger.exception("pull worker tick failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="flock-pull-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await self._task
        finally:
            self._task = None
