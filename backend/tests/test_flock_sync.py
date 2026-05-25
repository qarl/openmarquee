"""Unit tests for the flock sync engine (push + ingest)."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image

from openmarquee.content import TextSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.flock import FlockStorage
from openmarquee.flock_sync import FlockSync, PullWorker
from openmarquee.tombstone import TombstoneStorage

# `_NOW` was originally a hardcoded `datetime(2026, 4, 24, 12, 0, 0)`
# but the tombstone TTL is 30 days (per TOMBSTONE_TTL_DAYS in
# `openmarquee/tombstone.py`), so any test that ingests a tombstone
# stamped at `_NOW` ages out of `list_active()` once wall-clock
# wandered ~30 days past 2026-04-24. The fix mirrors qarl's
# 2026-05-20 c2eed27 anchor-to-now pattern on
# `test_pull_from_peer_applies_tombstones`: relative dates can't go
# stale, absolute dates inevitably do. Re-evaluated at module import
# (= test-run-start); per-test datetime arithmetic stays stable
# within a run.
_NOW = datetime.now(UTC).replace(microsecond=0)


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
async def test_ingest_delete_rolls_back_tombstone_on_content_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-16 correctness regression: parallel to api.py
    delete_content_item rollback. If self.content.delete raises mid-
    flight (NFS hiccup) after the tombstone is committed, _ingest_delete
    MUST roll the tombstone back so the next ingest pass can restart
    cleanly. Pre-fix the tombstone stayed committed and the local
    content stayed on disk; peers learned `deleted` via the persisted
    tombstone on next sync while local stayed inconsistent.
    """
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    slide = TextSlide(name="will fail mid-delete", text="x")
    content.save(slide, _make_png_bytes(), updated_at=_NOW - timedelta(hours=1))
    assert content.exists(slide.id)

    # Pre-condition: no tombstone.
    assert not any(t.content_id == slide.id for t in tombstones.load().tombstones)

    # Force content.delete to raise mid-flight.
    def raising_delete(item_id):
        raise OSError("simulated mid-delete storage failure")

    monkeypatch.setattr(content, "delete", raising_delete)

    # ingest_push -> _ingest_delete should propagate the OSError after
    # rolling the tombstone back.
    with pytest.raises(OSError, match="simulated mid-delete"):
        await sync.ingest_push(slide.id, "deleted", "peer.ts.net", _NOW)

    # CRITICAL ASSERTION: tombstone was rolled back.
    assert not any(t.content_id == slide.id for t in tombstones.load().tombstones), (
        "tombstone must be rolled back when content.delete fails"
    )

    # Sanity: local content still present (the delete failed).
    monkeypatch.undo()
    assert content.exists(slide.id), "local content must remain since the simulated delete failed"


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
    peer_updated_at = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
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
            return httpx.Response(200, content=sender_png, headers={"content-type": "image/png"})
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
    peer_updated_at = datetime(2026, 4, 20, tzinfo=UTC)
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
        return httpx.Response(200, json={"schema_version": 1, "entries": [], "tombstones": []})

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
    peer_ts = datetime(2026, 4, 20, tzinfo=UTC)
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

    sync, _, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler), enabled=False)
    await sync.announce_sync_to_peer("peer.ts.net", True)
    assert calls == []


def test_apply_sync_announcement_flips_matching_peer(tmp_path: Path):
    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(lambda r: httpx.Response(204)))
    peer = flock.add(address="peer.ts.net")
    assert peer.sync is False
    ok = sync.apply_sync_announcement("peer.ts.net", True)
    assert ok is True
    assert flock.load().find(peer.id).sync is True


def test_apply_sync_announcement_rejects_unknown_sender(tmp_path: Path):
    sync, _, _, _ = _build_sync(tmp_path, httpx.MockTransport(lambda r: httpx.Response(204)))
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
            {"content_id": str(cid), "deleted_at": ts.isoformat()} for cid, ts in tombstones
        ],
    }


@pytest.mark.asyncio
async def test_pull_from_peer_fetches_missing_content(tmp_path: Path):
    remote_cid = uuid4()
    remote_slide = TextSlide(id=remote_cid, name="Remote", text="r")
    remote_ts = datetime(2026, 4, 20, tzinfo=UTC)
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
    # Dates are anchored to "now" so the test never goes stale: the
    # remote delete must land inside TombstoneStorage's 30-day TTL
    # window, or list_active() (the final assert) drops it. A
    # hardcoded 2026-04 date aged out of that window — the time-bomb
    # this replaces.
    remote_delete_at = datetime.now(UTC) - timedelta(days=2)
    local_updated_at = remote_delete_at - timedelta(days=5)
    # Local content the remote has tombstoned. updated_at is OLDER
    # than the delete, so the pull's last-write-wins rule applies the
    # tombstone instead of keeping the local copy.
    local_cid = uuid4()
    local_slide = TextSlide(id=local_cid, name="Gone", text="g")
    content.save(local_slide, _make_png_bytes(), updated_at=local_updated_at)

    # Rebuild the handler with the actual data bound in.
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/flock/manifest"):
            return httpx.Response(
                200, json=_manifest_with(tombstones=((local_cid, remote_delete_at),))
            )
        return httpx.Response(404)

    sync, content, tombstones, _ = _build_sync(tmp_path, httpx.MockTransport(handler))
    content.save(local_slide, _make_png_bytes(), updated_at=local_updated_at)

    await sync.pull_from_peer("peer.ts.net")
    assert not content.exists(local_cid)
    assert {t.content_id for t in tombstones.list_active()} == {local_cid}


def _pull_tombstone_handler(request):
    return httpx.Response(200, json=_manifest_with())


@pytest.mark.asyncio
async def test_apply_pulled_tombstone_rolls_back_on_content_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-20 correctness regression: mirrors r16's _ingest_delete
    fix on the PULL applier. If self.content.delete raises mid-flight
    (NFS hiccup) after the tombstone is committed,
    _apply_pulled_tombstone MUST roll the tombstone back so the next
    pull pass can restart cleanly.

    Pre-fix the tombstone stayed committed and the local content
    stayed on disk -- the next manifest serve included a `deleted`
    tombstone for content this device still served, re-propagating
    a stale delete through the flock; peers that re-pulled the
    live copy mid-window flapped.

    Test shape: pre-seed local content stamped OLDER than the pulled
    delete (so LWW skip doesn't fire); monkeypatch content.delete to
    raise OSError; call _apply_pulled_tombstone directly (so we
    isolate the rollback contract from the rest of pull_from_peer's
    error-handling); assert OSError propagates AND tombstone is
    absent post-rollback AND local content still present.
    """
    sync, content, tombstones, _ = _build_sync(
        tmp_path, httpx.MockTransport(lambda r: httpx.Response(204))
    )
    remote_delete_at = datetime.now(UTC) - timedelta(days=2)
    local_updated_at = remote_delete_at - timedelta(days=5)
    local_cid = uuid4()
    local_slide = TextSlide(id=local_cid, name="will fail mid-pull-delete", text="x")
    content.save(local_slide, _make_png_bytes(), updated_at=local_updated_at)
    assert content.exists(local_cid)

    # Pre-condition: no tombstone for this id.
    assert not any(t.content_id == local_cid for t in tombstones.load().tombstones)

    # Force content.delete to raise mid-flight.
    def raising_delete(item_id):
        raise OSError("simulated mid-pull-delete storage failure")

    monkeypatch.setattr(content, "delete", raising_delete)

    # _apply_pulled_tombstone should propagate the OSError after
    # rolling the tombstone back.
    with pytest.raises(OSError, match="simulated mid-pull-delete"):
        sync._apply_pulled_tombstone(local_cid, remote_delete_at)

    # CRITICAL ASSERTION: tombstone was rolled back. Pre-fix this
    # would FAIL -- tombstone committed before the destructive step,
    # never removed on partial failure -- and the next manifest
    # serve would re-propagate the stale delete.
    assert not any(t.content_id == local_cid for t in tombstones.load().tombstones), (
        "tombstone must be rolled back when content.delete fails "
        "(otherwise stale delete re-propagates through the flock)"
    )

    # Sanity: local content still present since the delete failed.
    monkeypatch.undo()
    assert content.exists(local_cid), "local content must remain since the simulated delete failed"


@pytest.mark.asyncio
async def test_pull_from_peer_skips_when_local_newer(tmp_path: Path):
    cid = uuid4()
    remote_ts = datetime(2026, 4, 10, tzinfo=UTC)
    local_ts = datetime(2026, 4, 20, tzinfo=UTC)
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
    assert all(u.endswith("/api/flock/manifest") or u.endswith("/api/settings") for u in fetched)
    assert content.load(cid).name == "Local wins"


@pytest.mark.asyncio
async def test_pull_from_peer_survives_single_entry_failure(tmp_path: Path):
    good_cid = uuid4()
    bad_cid = uuid4()
    ts = datetime(2026, 4, 20, tzinfo=UTC)
    good_slide = TextSlide(id=good_cid, name="Good", text="g")
    png = _make_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/flock/manifest"):
            return httpx.Response(200, json=_manifest_with((good_cid, ts), (bad_cid, ts)))
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

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler), enabled=False)
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

    sync, _, _, _ = _build_sync(tmp_path, httpx.MockTransport(handler), enabled=False)
    await sync.pull_from_peer("peer.ts.net")
    assert hits == []


# --- §13 introduction protocol (gossip-on-add) ---


@pytest.mark.asyncio
async def test_gossip_add_pings_new_peer_and_existing_peers(tmp_path: Path):
    """When a new peer B is added, A hello-pings B (with A's own
    address) AND each existing peer C/D/E (with B's address). After
    settling, every peer knows about every other peer."""
    calls: list[tuple[str, dict]] = []

    def handler(request):
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    # Existing peers C and D, plus the just-added new peer B.
    flock.add(address="c.ts.net")
    flock.add(address="d.ts.net")
    flock.add(address="b.ts.net")  # new

    await sync.gossip_add("b.ts.net")

    # Three POSTs total — one to B (with self), two to existing
    # peers (with B's address). The reciprocal-add hello carries
    # OUR self-address so B adds us back; the forward-notification
    # hellos carry B's address so the existing peers add B.
    by_url = sorted(calls, key=lambda x: x[0])
    assert [u for u, _ in by_url] == [
        "http://b.ts.net/api/flock/hello",
        "http://c.ts.net/api/flock/hello",
        "http://d.ts.net/api/flock/hello",
    ]
    payloads = {u: p for u, p in by_url}
    assert payloads["http://b.ts.net/api/flock/hello"] == {"address": "me.ts.net"}
    assert payloads["http://c.ts.net/api/flock/hello"] == {"address": "b.ts.net"}
    assert payloads["http://d.ts.net/api/flock/hello"] == {"address": "b.ts.net"}


@pytest.mark.asyncio
async def test_gossip_add_excludes_new_peer_from_forward_set(tmp_path: Path):
    """The new peer is in our flock at gossip_add time (the POST
    /api/flock handler added it before scheduling the gossip
    background task), but we shouldn't tell B about itself — only
    OTHER existing peers get the forward notification."""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    flock.add(address="b.ts.net")  # new + only peer

    await sync.gossip_add("b.ts.net")

    # Just the reciprocal-add to B with our address. No forward to
    # B about itself, since the existing-peers loop skips
    # new_peer_address.
    assert calls == ["http://b.ts.net/api/flock/hello"]


@pytest.mark.asyncio
async def test_gossip_add_excludes_self_from_forward_set(tmp_path: Path):
    """Defensive: if the operator typo'd OUR own address into another
    peer's flock entry (or this device added itself somehow), don't
    gossip the addition back to ourselves."""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    # Our self-address is "me.ts.net" per _build_sync default. If it
    # somehow ends up in our flock list, the gossip should skip it.
    flock.add(address="me.ts.net")
    flock.add(address="b.ts.net")  # new

    await sync.gossip_add("b.ts.net")

    # Just the reciprocal-add to B. The me.ts.net entry is excluded
    # from the forward fan-out.
    assert calls == ["http://b.ts.net/api/flock/hello"]


@pytest.mark.asyncio
async def test_gossip_add_self_exclude_handles_mixed_case_self_address(tmp_path: Path):
    """Stored peer addresses are lowercased on entry (flock.py::
    _normalize_address). _resolve_self_address is case-preserving
    (env override, socket.gethostname(), and the tailscale_hostname
    validator do not lowercase). Without normalizing sender at the
    comparison site, the defensive self-exclude guard silently
    bypasses for any device whose self-address contains uppercase --
    every gossip-add fan-out re-introduces this device to itself.

    Setup: self-address is mixed-case "MyDevice.ts.net"; the lowercase
    form "mydevice.ts.net" is in our flock (because operator-added
    addresses get lowercased on store). Adding a new peer should NOT
    gossip back to our own self entry.
    """
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(
        tmp_path, httpx.MockTransport(handler), self_address="MyDevice.ts.net"
    )
    flock.add(address="MyDevice.ts.net")  # stored as "mydevice.ts.net"
    flock.add(address="b.ts.net")  # new

    await sync.gossip_add("b.ts.net")

    # Just the reciprocal-add to B. Our own self entry (stored
    # lowercase) must be excluded even though sender is mixed case.
    assert calls == ["http://b.ts.net/api/flock/hello"]


@pytest.mark.asyncio
async def test_gossip_add_skips_when_no_self_address(tmp_path: Path):
    """A device without a configured tailnet hostname can't tell
    peers how to reach it. Skip gossip silently rather than send
    nonsense."""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(204)

    content = ContentStorage(tmp_path / "content")
    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    flock = FlockStorage(tmp_path / "flock.json")
    flock.add(address="b.ts.net")
    sync = FlockSync(
        content_storage=content,
        tombstone_storage=tombstones,
        flock_storage=flock,
        get_self_address=lambda: None,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0
        ),
    )

    await sync.gossip_add("b.ts.net")
    assert calls == []


@pytest.mark.asyncio
async def test_gossip_add_swallows_peer_errors(tmp_path: Path):
    """A peer being unreachable shouldn't fail the gossip — the
    other peers in the fan-out should still get hellos. Eventual
    consistency model."""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if "broken.ts.net" in str(request.url):
            raise httpx.ConnectError("boom")
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    flock.add(address="broken.ts.net")
    flock.add(address="ok.ts.net")
    flock.add(address="b.ts.net")  # new

    # Doesn't raise.
    await sync.gossip_add("b.ts.net")

    # All three were attempted (the broken one + the ok one + the
    # reciprocal to B). gather() with return_exceptions=True wraps
    # the failure cleanly.
    urls = sorted(calls)
    assert urls == [
        "http://b.ts.net/api/flock/hello",
        "http://broken.ts.net/api/flock/hello",
        "http://ok.ts.net/api/flock/hello",
    ]


def test_apply_hello_adds_new_peer(tmp_path: Path):
    """Inbound hello for an unknown address adds it to local flock."""
    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(lambda r: httpx.Response(204)))
    assert flock.load().peers == []
    added = sync.apply_hello("new.ts.net")
    assert added is True
    peers = flock.load().peers
    assert len(peers) == 1
    assert peers[0].address == "new.ts.net"


def test_apply_hello_idempotent_for_known_peer(tmp_path: Path):
    """Duplicate hello (race between reciprocal-add + forward-
    notification) is a no-op rather than a 409 — gossip introductions
    can land twice for the same peer in a 3+-device flock."""
    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(lambda r: httpx.Response(204)))
    flock.add(address="known.ts.net")
    added = sync.apply_hello("known.ts.net")
    assert added is False
    # Still one peer, no duplicate.
    assert len(flock.load().peers) == 1


def test_apply_hello_does_not_cascade(tmp_path: Path):
    """Critical loop-prevention invariant: receiving a hello does
    NOT trigger another gossip_add. Without this, A→B→A→B would
    ping-pong forever."""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(204)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    flock.add(address="existing.ts.net")
    sync.apply_hello("new.ts.net")
    # apply_hello should not have fired any HTTP — only gossip_add
    # does fan-out, and apply_hello explicitly doesn't call it.
    assert calls == []


# --- Phase B.3: out-of-sync diff (items_behind tracking) ---


@pytest.mark.asyncio
async def test_pull_from_peer_records_items_behind_pre_apply(tmp_path: Path):
    """Phase B.3: pull_from_peer counts manifest entries we don't have
    BEFORE applying them, stamps the count onto the peer record.
    Operator's read of 'K items behind' is the moment-of-pull gap."""
    cid_a = uuid4()
    cid_b = uuid4()
    cid_c = uuid4()
    remote_ts = datetime(2026, 4, 20, tzinfo=UTC)
    png = _make_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/flock/manifest"):
            return httpx.Response(
                200,
                json=_manifest_with((cid_a, remote_ts), (cid_b, remote_ts), (cid_c, remote_ts)),
            )
        if "/api/content/" in url and url.endswith("/asset"):
            return httpx.Response(200, content=png)
        if "/api/content/" in url:
            slide = TextSlide(id=UUID(url.rsplit("/", 1)[-1]), name="r", text="r")
            return httpx.Response(200, json=slide.model_dump(mode="json"))
        return httpx.Response(404)

    sync, content, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    peer = flock.add(address="peer.ts.net")
    # Pre-seed one of the three so the count comes out to 2-not-3.
    content.save(
        TextSlide(id=cid_a, name="had", text="had"),
        _make_png_bytes(),
        updated_at=datetime(2026, 4, 19, tzinfo=UTC),
    )

    await sync.pull_from_peer("peer.ts.net")

    refreshed = flock.load().find(peer.id)
    assert refreshed is not None
    # Two missing at moment-of-pull (cid_b + cid_c). cid_a was
    # already local. Computed pre-apply per the spec comment.
    assert refreshed.items_behind == 2


@pytest.mark.asyncio
async def test_pull_from_peer_records_zero_when_in_sync(tmp_path: Path):
    """If we already have everything in the peer's manifest at pull
    time, items_behind = 0. UI surfaces this as 'in sync'."""
    cid = uuid4()
    remote_ts = datetime(2026, 4, 20, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/api/flock/manifest"):
            return httpx.Response(200, json=_manifest_with((cid, remote_ts)))
        return httpx.Response(404)

    sync, content, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    peer = flock.add(address="peer.ts.net")
    content.save(
        TextSlide(id=cid, name="had", text="had"),
        _make_png_bytes(),
        updated_at=datetime(2026, 4, 19, tzinfo=UTC),
    )

    await sync.pull_from_peer("peer.ts.net")

    assert flock.load().find(peer.id).items_behind == 0


@pytest.mark.asyncio
async def test_pull_from_peer_leaves_items_behind_unchanged_on_manifest_failure(
    tmp_path: Path,
):
    """Manifest fetch failure → pull aborts before recording. The
    previously-stored items_behind is NOT zeroed out — operator's
    'last known' value stays visible until the next successful pull
    actually computes a fresh number."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    sync, _, _, flock = _build_sync(tmp_path, httpx.MockTransport(handler))
    peer = flock.add(address="peer.ts.net")
    # Pre-stamp a known value to verify it survives the failed pull.
    flock.update(peer.id, items_behind=7)

    await sync.pull_from_peer("peer.ts.net")

    assert flock.load().find(peer.id).items_behind == 7
