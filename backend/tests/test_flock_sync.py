"""Unit tests for the flock sync engine (push + ingest)."""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from openmarquee.content import TextSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.flock import FlockStorage
from openmarquee.flock_sync import FlockSync, PullWorker
from openmarquee.tombstone import TombstoneStorage


_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)


def _make_png_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, "PNG")
    return buf.getvalue()


def _build_sync(
    tmp_path: Path,
    transport: httpx.MockTransport,
    self_address: str = "me.ts.net",
    enabled: bool = True,
) -> tuple[FlockSync, ContentStorage, TombstoneStorage, FlockStorage]:
    content = ContentStorage(tmp_path / "content")
    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    flock = FlockStorage(tmp_path / "flock.json")

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    sync = FlockSync(
        content_storage=content,
        tombstone_storage=tombstones,
        flock_storage=flock,
        get_self_address=lambda: self_address,
        get_sync_enabled=lambda: enabled,
        http_client_factory=factory,
    )
    return sync, content, tombstones, flock


# --- outbound push ---


@pytest.mark.asyncio
async def test_notify_peers_skips_when_no_sync_true_peers(tmp_path: Path):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    # Add a peer but leave sync=False.
    flock.add(address="peer.ts.net")
    await sync.notify_peers(uuid4(), "updated")
    assert calls == []


@pytest.mark.asyncio
async def test_notify_peers_pushes_to_each_sync_peer(tmp_path: Path):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    a = flock.add(address="a.ts.net")
    b = flock.add(address="b.ts.net")
    flock.update(a.id, sync=True)
    flock.update(b.id, sync=True)

    cid = uuid4()
    await sync.notify_peers(cid, "updated")
    hits = sorted(str(c.url) for c in calls)
    assert hits == [
        "http://a.ts.net/api/flock/notify",
        "http://b.ts.net/api/flock/notify",
    ]


@pytest.mark.asyncio
async def test_notify_peers_swallows_peer_errors(tmp_path: Path):
    # If peer A is down, B still gets pushed to — push is best-effort.
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        if "a.ts.net" in str(request.url):
            raise httpx.ConnectError("unreachable")
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    a = flock.add(address="a.ts.net")
    b = flock.add(address="b.ts.net")
    flock.update(a.id, sync=True)
    flock.update(b.id, sync=True)

    await sync.notify_peers(uuid4(), "updated")  # does not raise
    targets = sorted(str(c.url) for c in calls)
    assert targets == [
        "http://a.ts.net/api/flock/notify",
        "http://b.ts.net/api/flock/notify",
    ]


@pytest.mark.asyncio
async def test_notify_peers_skips_when_no_self_address(tmp_path: Path):
    # Device hasn't been given a reachable hostname yet → can't push.
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    content = ContentStorage(tmp_path / "content")
    tombstones = TombstoneStorage(tmp_path / "t.json")
    flock = FlockStorage(tmp_path / "f.json")
    peer = flock.add(address="peer.ts.net")
    flock.update(peer.id, sync=True)

    sync = FlockSync(
        content_storage=content,
        tombstone_storage=tombstones,
        flock_storage=flock,
        get_self_address=lambda: None,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0
        ),
    )
    await sync.notify_peers(uuid4(), "updated")
    assert calls == []


# --- inbound ingest ---


@pytest.mark.asyncio
async def test_ingest_delete_records_tombstone_and_removes_local(tmp_path: Path):
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    slide = TextSlide(name="X", text="x")
    content.save(slide, _make_png_bytes(), updated_at=_NOW - timedelta(hours=1))
    assert content.exists(slide.id)

    await sync.ingest_push(slide.id, "deleted", "peer.ts.net", _NOW)
    assert not content.exists(slide.id)
    assert {t.content_id for t in tombstones.list_active()} == {slide.id}


@pytest.mark.asyncio
async def test_ingest_delete_idempotent_when_content_absent(tmp_path: Path):
    # Delete push arrives for something we never had — still mint the
    # tombstone so our peers (if any) learn about the deletion too.
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    cid = uuid4()
    await sync.ingest_push(cid, "deleted", "peer.ts.net", _NOW)
    assert not content.exists(cid)
    assert {t.content_id for t in tombstones.list_active()} == {cid}


@pytest.mark.asyncio
async def test_ingest_delete_skipped_when_local_edit_is_newer(tmp_path: Path):
    # LWW: delete push stamped at T; local edit stamped at T+1. Keep the
    # edit, don't record a tombstone (would re-propagate the stale delete
    # on the next sync round).
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    slide = TextSlide(name="Edit wins", text="mine")
    local_edit = _NOW + timedelta(hours=1)
    content.save(slide, _make_png_bytes(), updated_at=local_edit)

    await sync.ingest_push(slide.id, "deleted", "peer.ts.net", _NOW)
    assert content.exists(slide.id)
    assert content.load(slide.id).name == "Edit wins"
    assert tombstones.list_active() == []


@pytest.mark.asyncio
async def test_ingest_update_pulls_content_and_saves_with_sender_timestamp(
    tmp_path: Path,
):
    peer_cid = uuid4()
    peer_updated_at = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    sender_slide = TextSlide(id=peer_cid, name="From Peer", text="hello")
    sender_png = _make_png_bytes((255, 0, 128))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://peer.ts.net/api/flock/manifest":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "entries": [
                        {
                            "content_id": str(peer_cid),
                            "content_type": "text_slide",
                            "updated_at": peer_updated_at.isoformat(),
                        }
                    ],
                    "tombstones": [],
                },
            )
        if url == f"http://peer.ts.net/api/content/{peer_cid}":
            return httpx.Response(200, json=sender_slide.model_dump(mode="json"))
        if url == f"http://peer.ts.net/api/content/{peer_cid}/asset":
            return httpx.Response(
                200, content=sender_png, headers={"content-type": "image/png"}
            )
        return httpx.Response(404)

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.ingest_push(peer_cid, "updated", "peer.ts.net", _NOW)

    assert content.exists(peer_cid)
    loaded = content.load(peer_cid)
    assert loaded.id == peer_cid
    assert loaded.name == "From Peer"
    # Sender's updated_at is preserved — not now().
    assert content.read_updated_at(peer_cid) == peer_updated_at
    # Asset bytes round-trip exactly.
    assert content.read_asset(peer_cid) == sender_png


@pytest.mark.asyncio
async def test_ingest_update_skips_when_local_is_newer(tmp_path: Path):
    # Sender's stamp is BEFORE our local stamp → last-writer-wins keeps us.
    cid = uuid4()
    peer_updated_at = datetime(2026, 4, 20, tzinfo=timezone.utc)
    local_updated_at = peer_updated_at + timedelta(hours=1)
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if str(request.url).endswith("/api/flock/manifest"):
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "entries": [
                        {
                            "content_id": str(cid),
                            "content_type": "text_slide",
                            "updated_at": peer_updated_at.isoformat(),
                        }
                    ],
                    "tombstones": [],
                },
            )
        return httpx.Response(500)  # Would never fire if skip works.

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    local_slide = TextSlide(id=cid, name="Local edit", text="mine")
    content.save(local_slide, _make_png_bytes(), updated_at=local_updated_at)

    await sync.ingest_push(cid, "updated", "peer.ts.net", _NOW)
    # Manifest was fetched; content + asset were NOT.
    assert all("manifest" in u for u in fetched)
    # Local copy is untouched.
    assert content.load(cid).name == "Local edit"
    assert content.read_updated_at(cid) == local_updated_at


@pytest.mark.asyncio
async def test_ingest_update_skips_when_sender_evicted(tmp_path: Path):
    # Manifest doesn't list the pushed id — sender deleted between push
    # and our pull. No-op.
    cid = uuid4()

    def handler(request):
        return httpx.Response(
            200, json={"schema_version": 1, "entries": [], "tombstones": []}
        )

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.ingest_push(cid, "updated", "peer.ts.net", _NOW)
    assert not content.exists(cid)


# --- video ingest branch ---


def _make_fake_mp4_bytes() -> bytes:
    # Minimal MP4 frame: enough to pass the `ftyp` sniff in api.py's
    # decode path. Tests of flock_sync exercise the ingest path, which
    # writes the bytes verbatim — no validation on this side.
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 24


@pytest.mark.asyncio
async def test_ingest_update_handles_video_with_separate_mp4_fetch(tmp_path: Path):
    peer_cid = uuid4()
    peer_ts = datetime(2026, 4, 20, tzinfo=timezone.utc)
    sender_video = VideoSlide(id=peer_cid, name="From Peer", duration_ms=3000)
    png = _make_png_bytes()
    mp4 = _make_fake_mp4_bytes()
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hits.append(url)
        if url == "http://peer.ts.net/api/flock/manifest":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "entries": [
                        {
                            "content_id": str(peer_cid),
                            "content_type": "video",
                            "updated_at": peer_ts.isoformat(),
                        }
                    ],
                    "tombstones": [],
                },
            )
        if url == f"http://peer.ts.net/api/content/{peer_cid}":
            return httpx.Response(200, json=sender_video.model_dump(mode="json"))
        if url == f"http://peer.ts.net/api/content/{peer_cid}/asset":
            return httpx.Response(200, content=png)
        if url == f"http://peer.ts.net/api/content/{peer_cid}/video":
            return httpx.Response(200, content=mp4)
        return httpx.Response(404)

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.ingest_push(peer_cid, "updated", "peer.ts.net", _NOW)

    assert content.exists(peer_cid)
    assert content.read_asset(peer_cid) == png
    assert content.read_video(peer_cid) == mp4
    assert content.read_updated_at(peer_cid) == peer_ts
    # The /video endpoint was hit — the video branch ran, not the image one.
    assert any(u.endswith("/video") for u in hits)


# --- sync-flag announcement ---


@pytest.mark.asyncio
async def test_announce_sync_to_peer_posts_to_expected_endpoint(tmp_path: Path):
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        calls.append((str(request.url), _json.loads(request.content)))
        return httpx.Response(204)

    sync, _, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.announce_sync_to_peer("peer.ts.net", True)
    assert calls == [
        (
            "http://peer.ts.net/api/flock/sync-announce",
            {"sender_address": "me.ts.net", "sync": True},
        )
    ]


@pytest.mark.asyncio
async def test_announce_sync_skipped_when_sync_disabled(tmp_path: Path):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    sync, _, _, _ = _build_sync(
        tmp_path, httpx.MockTransport(handler), enabled=False
    )
    await sync.announce_sync_to_peer("peer.ts.net", True)
    assert calls == []


def test_apply_sync_announcement_flips_matching_peer(tmp_path: Path):
    sync, _, _, flock = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    peer = flock.add(address="peer.ts.net")
    assert peer.sync is False
    ok = sync.apply_sync_announcement("peer.ts.net", True)
    assert ok is True
    assert flock.load().find(peer.id).sync is True


def test_apply_sync_announcement_rejects_unknown_sender(tmp_path: Path):
    sync, _, _, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    assert sync.apply_sync_announcement("stranger.ts.net", True) is False


# --- peer-name probe ---


@pytest.mark.asyncio
async def test_probe_peer_name_updates_flock_entry(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/settings"):
            return httpx.Response(200, json={"sign_name": "Lobby Sign"})
        return httpx.Response(404)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    peer = flock.add(address="peer.ts.net")
    assert peer.name is None

    await sync.probe_peer_name("peer.ts.net")
    refreshed = flock.load().find(peer.id)
    assert refreshed.name == "Lobby Sign"


@pytest.mark.asyncio
async def test_probe_peer_name_ignores_unreachable_remote(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    peer = flock.add(address="offline.ts.net")
    await sync.probe_peer_name("offline.ts.net")  # must not raise
    assert flock.load().find(peer.id).name is None


@pytest.mark.asyncio
async def test_probe_peer_name_skips_missing_peer(tmp_path: Path):
    # Nothing should happen (and certainly no HTTP call) if the
    # address isn't in our flock.
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, json={"sign_name": "X"})

    sync, _, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.probe_peer_name("stranger.ts.net")
    assert hits == []


# --- periodic pull worker ---


def _manifest_with(*entries, tombstones=()):
    return {
        "schema_version": 1,
        "entries": [
            {
                "content_id": str(cid),
                "content_type": "text_slide",
                "updated_at": ts.isoformat(),
            }
            for cid, ts in entries
        ],
        "tombstones": [
            {"content_id": str(cid), "deleted_at": ts.isoformat()}
            for cid, ts in tombstones
        ],
    }


@pytest.mark.asyncio
async def test_pull_from_peer_fetches_missing_content(tmp_path: Path):
    remote_cid = uuid4()
    remote_slide = TextSlide(id=remote_cid, name="Remote", text="r")
    remote_ts = datetime(2026, 4, 20, tzinfo=timezone.utc)
    png = _make_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/flock/manifest"):
            return httpx.Response(200, json=_manifest_with((remote_cid, remote_ts)))
        if url.endswith(f"/api/content/{remote_cid}"):
            return httpx.Response(200, json=remote_slide.model_dump(mode="json"))
        if url.endswith(f"/api/content/{remote_cid}/asset"):
            return httpx.Response(200, content=png)
        return httpx.Response(404)

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.pull_from_peer("peer.ts.net")
    assert content.exists(remote_cid)
    assert content.read_updated_at(remote_cid) == remote_ts


@pytest.mark.asyncio
async def test_pull_from_peer_applies_tombstones(tmp_path: Path):
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: _pull_tombstone_handler(r))
    )
    # Local content that the remote has tombstoned.
    local_cid = uuid4()
    local_slide = TextSlide(id=local_cid, name="Gone", text="g")
    content.save(
        local_slide,
        _make_png_bytes(),
        updated_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )
    remote_delete_at = datetime(2026, 4, 15, tzinfo=timezone.utc)

    # Rebuild the handler with the actual data bound in.
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/flock/manifest"):
            return httpx.Response(
                200, json=_manifest_with(tombstones=((local_cid, remote_delete_at),))
            )
        return httpx.Response(404)

    sync, content, tombstones, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    content.save(
        local_slide,
        _make_png_bytes(),
        updated_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
    )

    await sync.pull_from_peer("peer.ts.net")
    assert not content.exists(local_cid)
    assert {t.content_id for t in tombstones.list_active()} == {local_cid}


def _pull_tombstone_handler(request):
    return httpx.Response(200, json=_manifest_with())


@pytest.mark.asyncio
async def test_pull_from_peer_skips_when_local_newer(tmp_path: Path):
    cid = uuid4()
    remote_ts = datetime(2026, 4, 10, tzinfo=timezone.utc)
    local_ts = datetime(2026, 4, 20, tzinfo=timezone.utc)
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if str(request.url).endswith("/api/flock/manifest"):
            return httpx.Response(200, json=_manifest_with((cid, remote_ts)))
        return httpx.Response(500)

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    local = TextSlide(id=cid, name="Local wins", text="mine")
    content.save(local, _make_png_bytes(), updated_at=local_ts)

    await sync.pull_from_peer("peer.ts.net")
    # Only the peer-name probe + the manifest — content + asset NOT fetched.
    assert all(
        u.endswith("/api/flock/manifest") or u.endswith("/api/settings")
        for u in fetched
    )
    assert content.load(cid).name == "Local wins"


@pytest.mark.asyncio
async def test_pull_from_peer_survives_single_entry_failure(tmp_path: Path):
    good_cid = uuid4()
    bad_cid = uuid4()
    ts = datetime(2026, 4, 20, tzinfo=timezone.utc)
    good_slide = TextSlide(id=good_cid, name="Good", text="g")
    png = _make_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/flock/manifest"):
            return httpx.Response(
                200, json=_manifest_with((good_cid, ts), (bad_cid, ts))
            )
        if url.endswith(f"/api/content/{good_cid}"):
            return httpx.Response(200, json=good_slide.model_dump(mode="json"))
        if url.endswith(f"/api/content/{good_cid}/asset"):
            return httpx.Response(200, content=png)
        # bad_cid fetches return 500 — the good one should still land.
        return httpx.Response(500)

    sync, content, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.pull_from_peer("peer.ts.net")
    assert content.exists(good_cid)
    assert not content.exists(bad_cid)


@pytest.mark.asyncio
async def test_pull_from_peer_handles_manifest_unreachable(tmp_path: Path):
    # Peer is offline — no exception bubbles.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    sync, _, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    await sync.pull_from_peer("offline.ts.net")


@pytest.mark.asyncio
async def test_pull_worker_ticks_on_interval_and_stops_cleanly(tmp_path: Path):
    ticks: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ticks.append(str(request.url))
        return httpx.Response(200, json=_manifest_with())

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    peer = flock.add(address="peer.ts.net")
    flock.update(peer.id, sync=True)

    worker = PullWorker(sync, interval_seconds=0.05)
    await worker.start()
    # One tick fires immediately; wait for a second.
    await asyncio.sleep(0.15)
    await worker.stop()

    # Each tick hits /api/settings (name probe) + /api/flock/manifest.
    manifest_hits = [u for u in ticks if u.endswith("/api/flock/manifest")]
    assert len(manifest_hits) >= 2


@pytest.mark.asyncio
async def test_pull_worker_double_start_is_noop(tmp_path: Path):
    sync, _, _, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(200, json=_manifest_with()))
    )
    worker = PullWorker(sync, interval_seconds=60.0)
    await worker.start()
    task1 = worker._task
    await worker.start()
    assert worker._task is task1
    await worker.stop()


# --- global kill switch (SystemSettings.flock_sync_enabled) ---


@pytest.mark.asyncio
async def test_notify_peers_noops_when_sync_disabled(tmp_path: Path):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(
        tmp_path, httpx.MockTransport(handler), enabled=False
    )
    peer = flock.add(address="peer.ts.net")
    flock.update(peer.id, sync=True)
    await sync.notify_peers(uuid4(), "updated")
    assert calls == []


@pytest.mark.asyncio
async def test_ingest_push_noops_when_sync_disabled(tmp_path: Path):
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204)), enabled=False
    )
    await sync.ingest_push(uuid4(), "deleted", "peer.ts.net", _NOW)
    # No tombstone recorded; ingest should have silently dropped.
    assert tombstones.list_active() == []


@pytest.mark.asyncio
async def test_pull_from_peer_noops_when_sync_disabled(tmp_path: Path):
    hits: list[str] = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200, json={"schema_version": 1, "entries": [], "tombstones": []})

    sync, _, _, _ = _build_sync(
        tmp_path, httpx.MockTransport(handler), enabled=False
    )
    await sync.pull_from_peer("peer.ts.net")
    assert hits == []
