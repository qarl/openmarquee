"""Tests for atomic-write rollback across all file-backed storages.

Each storage class persists state via the tmp.write_text +
tmp.replace(path) pattern. Sweep #3 found that only save_video had
a partial-failure rollback test; the rest were untested for the
"replace() failed mid-flight" failure mode. Silent data loss is
the worst-case fingerprint: save returned success but the file is
empty / missing.

Test pattern (per class):
  1. Save baseline state v1; verify load returns it.
  2. Monkeypatch Path.replace to raise OSError when called on a
     `.tmp` path (the atomic-rename step).
  3. Attempt save v2; expect OSError to propagate.
  4. Verify load() still returns v1 (no data loss).
  5. Verify no leftover .tmp file exists.

The monkeypatch is scoped via the per-test `monkeypatch` fixture
so it doesn't bleed across tests.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.content import (
    ImageSlide,
    TextLayer,
    TextSlide,
)
from openmarquee.content.storage import ContentStorage
from openmarquee.flock import FlockStorage
from openmarquee.playlist import (
    DEFAULT_PLAYLIST_ID,
    DEFAULT_PLAYLIST_NAME,
    Playlist,
    PlaylistCollection,
    PlaylistStorage,
)
from openmarquee.schedule import Schedule, ScheduleStorage
from openmarquee.settings import SettingsStorage, SystemSettings
from openmarquee.tombstone import TombstoneStorage


def _fake_png() -> bytes:
    """Minimal valid PNG header for tests that don't need actual
    image bytes (storage doesn't decode them)."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _no_leftover_tmp_files(root: Path) -> bool:
    """True if there's no atomic-write staging file anywhere under
    `root`. Round-29: the staging-file suffix changed from a fixed
    ".tmp" to per-call ".tmp.<pid>.<hex>" so the matcher checks
    `.tmp` SUBSTRING (rather than endswith) to catch both old and
    new shapes."""
    return not any(".tmp" in p.name for p in root.rglob("*"))


def _patch_replace_to_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch Path.replace to raise OSError only when invoked on an
    atomic-write staging path. Saves to other paths (e.g.
    tmp_path/seed_marker writes via different APIs) aren't affected.

    Round-29: staging suffix is per-call ".tmp.<pid>.<hex>", so we
    match the .tmp SUBSTRING rather than endswith."""
    original_replace = Path.replace

    def _raise_if_tmp(self: Path, target):
        if ".tmp." in self.name:
            raise OSError(28, "simulated disk full (errno=ENOSPC)")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _raise_if_tmp)


# --- ContentStorage ---------------------------------------------------------


def test_content_storage_save_rollback_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """ContentStorage.save: when tmp.replace fails mid-save, the
    original envelope+asset remain readable on disk + no .tmp
    files linger."""
    storage = ContentStorage(tmp_path / "content")
    slide_v1 = TextSlide(
        name="v1",
        duration_ms=5000,
        text_layers=[TextLayer(text="version 1")],
    )
    storage.save(slide_v1, _fake_png())
    loaded_v1 = storage.load(slide_v1.id)
    assert loaded_v1.text_layers[0].text == "version 1"

    _patch_replace_to_raise(monkeypatch)

    slide_v2 = TextSlide(
        id=slide_v1.id,
        name="v2",
        duration_ms=5000,
        text_layers=[TextLayer(text="version 2")],
    )
    with pytest.raises(OSError):
        storage.save(slide_v2, _fake_png())

    # Original on-disk envelope must be unchanged; the cache pop
    # before write means a fresh load() re-reads disk.
    loaded_after = storage.load(slide_v1.id)
    assert loaded_after.text_layers[0].text == "version 1"
    assert _no_leftover_tmp_files(tmp_path)


# --- PlaylistStorage --------------------------------------------------------


def test_playlist_storage_save_all_rollback_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    v1 = PlaylistCollection(
        playlists=[
            Playlist(
                id=DEFAULT_PLAYLIST_ID,
                name=DEFAULT_PLAYLIST_NAME,
                items=[],
            ),
        ]
    )
    storage.save_all(v1)
    assert storage.load_all().playlists[0].name == DEFAULT_PLAYLIST_NAME

    _patch_replace_to_raise(monkeypatch)

    v2 = PlaylistCollection(
        playlists=[
            Playlist(
                id=DEFAULT_PLAYLIST_ID,
                name="renamed-attempt",
                items=[],
            ),
        ]
    )
    with pytest.raises(OSError):
        storage.save_all(v2)

    # Cache was cleared BEFORE the failed write (Batch 7.2 fix);
    # load_all re-reads disk and gets the original v1 back.
    assert storage.load_all().playlists[0].name == DEFAULT_PLAYLIST_NAME
    assert _no_leftover_tmp_files(tmp_path)


# --- FlockStorage -----------------------------------------------------------


def test_flock_storage_save_rollback_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = FlockStorage(tmp_path / "flock.json")
    # Add a baseline peer.
    storage.add("100.64.0.1")
    assert any(p.address == "100.64.0.1" for p in storage.load().peers)

    _patch_replace_to_raise(monkeypatch)

    with pytest.raises(OSError):
        storage.add("100.64.0.2")  # second peer, will fail mid-save

    # Original flock state intact (cache cleared pre-write per 7.2).
    after = storage.load()
    assert any(p.address == "100.64.0.1" for p in after.peers)
    assert not any(p.address == "100.64.0.2" for p in after.peers)
    assert _no_leftover_tmp_files(tmp_path)


# --- ScheduleStorage --------------------------------------------------------


def test_schedule_storage_save_rollback_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = ScheduleStorage(tmp_path / "schedule.json")
    v1 = Schedule()  # default empty
    storage.save(v1)
    assert storage.load().rules == []

    _patch_replace_to_raise(monkeypatch)

    # ScheduleStorage.save: mutating + saving a non-empty schedule
    # would require a full Schedule construction; for the rollback
    # invariant we just try to save() the same shape and assert it
    # raises + leaves no .tmp.
    with pytest.raises(OSError):
        storage.save(v1)
    assert _no_leftover_tmp_files(tmp_path)


# --- SettingsStorage --------------------------------------------------------


def test_settings_storage_save_rollback_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = SettingsStorage(tmp_path / "settings.json")
    v1 = storage.load()  # triggers first-write
    original_sign_name = v1.sign_name
    assert (tmp_path / "settings.json").exists()

    _patch_replace_to_raise(monkeypatch)

    v2 = v1.model_copy(update={"sign_name": "Sign999"})
    with pytest.raises(OSError):
        storage.save(v2)

    # Re-load: disk still has v1.
    loaded = storage.load()
    assert loaded.sign_name == original_sign_name
    assert _no_leftover_tmp_files(tmp_path)


# --- TombstoneStorage -------------------------------------------------------


def test_tombstone_storage_save_rollback_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = TombstoneStorage(tmp_path / "tombstones.json")
    id_a = uuid4()
    storage.add(id_a)
    log_v1 = storage.load()
    assert any(t.content_id == id_a for t in log_v1.tombstones)

    _patch_replace_to_raise(monkeypatch)

    id_b = uuid4()
    with pytest.raises(OSError):
        storage.add(id_b)

    after = storage.load()
    assert any(t.content_id == id_a for t in after.tombstones)
    assert not any(t.content_id == id_b for t in after.tombstones)
    assert _no_leftover_tmp_files(tmp_path)


# --- Batch 11.2 / sweep #5 #6: every storage class saves at 0600 ---
#
# settings.json is the one that actually motivates 0600 (AP password,
# station password, future Tailscale auth key), but the helper applies
# uniformly so every storage class inherits the discipline. A
# parameterized sweep keeps a future contributor from leaking a new
# storage class with a different umask-default mode.


def _save_baseline(storage, tmp_path: Path):
    """Save a fresh baseline through each storage's save API."""
    cls = type(storage).__name__
    if cls == "ContentStorage":
        slide = ImageSlide(name="m", duration_ms=1000)
        storage.save_image(slide, _fake_png())
        return tmp_path / str(slide.id) / "item.json"
    if cls == "PlaylistStorage":
        coll = PlaylistCollection(
            playlists=[Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME, items=[])]
        )
        storage.save_all(coll)
        return storage.path
    if cls == "FlockStorage":
        storage.save(storage.load())
        return storage.path
    if cls == "ScheduleStorage":
        storage.save(Schedule())
        return storage.path
    if cls == "SettingsStorage":
        storage.save(SystemSettings())
        return storage.path
    if cls == "TombstoneStorage":
        storage.add(uuid4())
        return storage.path
    raise AssertionError(f"unknown storage class: {cls}")


@pytest.mark.parametrize(
    "factory",
    [
        lambda p: ContentStorage(p),
        lambda p: PlaylistStorage(p / "playlist.json"),
        lambda p: FlockStorage(p / "flock.json"),
        lambda p: ScheduleStorage(p / "schedule.json"),
        lambda p: SettingsStorage(p / "settings.json"),
        lambda p: TombstoneStorage(p / "tombstones.json"),
    ],
    ids=[
        "ContentStorage",
        "PlaylistStorage",
        "FlockStorage",
        "ScheduleStorage",
        "SettingsStorage",
        "TombstoneStorage",
    ],
)
def test_storage_save_lands_at_0600(factory, tmp_path: Path):
    storage = factory(tmp_path)
    written = _save_baseline(storage, tmp_path)
    assert written.exists()
    mode = written.stat().st_mode & 0o777
    assert mode == 0o600, f"{written.name} mode is {oct(mode)}, want 0o600"
