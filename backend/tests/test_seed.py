"""Unit tests for first-boot content seeding."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from openmarquee.content import ImageSlide, TextSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.playlist import PlaylistStorage
from openmarquee.seed import (
    SEED_VERSION,
    SeedPreset,
    render_gradient_png,
    seed_if_needed,
)


_FAKE_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 120


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def playlist(tmp_path: Path) -> PlaylistStorage:
    return PlaylistStorage(tmp_path / "playlist.json")


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "seeded.json"


# --- render_gradient_png ---


def test_render_gradient_png_returns_valid_png_at_requested_size():
    preset = SeedPreset(name="x", top=(10, 20, 30), bottom=(200, 100, 50))
    png = render_gradient_png(preset, 64, 48)
    img = Image.open(io.BytesIO(png))
    img.verify()
    img = Image.open(io.BytesIO(png))  # re-open: verify() exhausts the stream
    assert img.size == (64, 48)
    assert img.mode == "RGB"


def test_gradient_interpolates_from_top_to_bottom():
    preset = SeedPreset(name="x", top=(255, 0, 0), bottom=(0, 0, 255))
    png = render_gradient_png(preset, 4, 8)
    img = Image.open(io.BytesIO(png))
    top_row = [img.getpixel((x, 0)) for x in range(4)]
    bottom_row = [img.getpixel((x, 7)) for x in range(4)]
    assert all(p == (255, 0, 0) for p in top_row)
    assert all(p == (0, 0, 255) for p in bottom_row)


# --- seed_if_needed: happy path ---


def test_seed_creates_starter_slides_when_fresh(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    created = seed_if_needed(storage, playlist, marker, width=32, height=32)

    assert len(created) >= 3
    # All are ImageSlide — seeded as images, not text slides.
    assert all(isinstance(s, ImageSlide) for s in created)

    # And they round-trip through storage.
    loaded = storage.list_all()
    assert len(loaded) == len(created)

    # Every seeded item landed in the default playlist, in order.
    ids_in_playlist = playlist.load().item_ids
    assert ids_in_playlist == [s.id for s in created]


def test_seed_writes_marker_file_recording_what_it_did(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    created = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert marker.exists()
    payload = json.loads(marker.read_text())
    assert payload["seed_version"] == SEED_VERSION
    assert payload["created"] == len(created)
    assert payload["reason"] == "fresh-install"


# --- seed_if_needed: no-op cases ---


def test_seed_is_noop_when_marker_already_present(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"seed_version": 1, "created": 0, "reason": "manual"}))
    created = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert created == []
    assert storage.list_all() == []


def test_seed_is_noop_when_content_already_present(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """Operator-uploaded content takes priority — seeding must not touch it."""
    storage.save_text_slide(TextSlide(name="mine", text="mine"), b"\x89PNG")
    created = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert created == []
    # Marker gets stamped so we don't keep re-checking on every boot.
    assert marker.exists()
    assert json.loads(marker.read_text())["reason"] == "content-already-present"


def test_seed_respects_marker_even_if_operator_later_deletes_all_content(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """Deleting everything shouldn't trigger re-seeding on next boot."""
    # Seed once.
    first = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert first

    # Operator wipes all content.
    for item in storage.list_all():
        storage.delete(item.id)
    playlist.save(playlist.load())  # no-op save to touch the file

    # Next boot: marker still present → no re-seed.
    second = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert second == []


def test_seed_failure_does_not_stamp_marker(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path, monkeypatch
):
    """If seeding blows up partway, the marker stays absent so next boot
    gets another shot at seeding (rather than leaving the operator with
    a half-populated fresh device)."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk pressure")

    monkeypatch.setattr("openmarquee.seed.render_gradient_png", boom)
    with pytest.raises(RuntimeError):
        seed_if_needed(storage, playlist, marker, width=16, height=16)

    assert not marker.exists()


def test_seed_rolls_back_partial_items_on_mid_loop_failure(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path, monkeypatch
):
    """If preset #3 raises after #1 and #2 saved, clean up #1 and #2
    before re-raising so we don't leave orphans that later boots would
    mis-interpret as 'operator content present'."""
    from openmarquee import seed as seed_module

    real_render = seed_module.render_gradient_png
    call_count = {"n": 0}

    def flaky(preset, w, h):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise RuntimeError("disk pressure")
        return real_render(preset, w, h)

    monkeypatch.setattr(seed_module, "render_gradient_png", flaky)
    with pytest.raises(RuntimeError):
        seed_if_needed(storage, playlist, marker, width=16, height=16)

    # Partial items were cleaned up; no marker.
    assert storage.list_all() == []
    assert not marker.exists()


# --- demo video ---


def test_seed_registers_demo_video_when_mp4_is_present(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(_FAKE_MP4)

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=16,
        height=16,
        demo_video_path=video_path,
    )

    videos = [s for s in created if isinstance(s, VideoSlide)]
    assert len(videos) == 1
    assert "Demo" in videos[0].name
    # And it round-trips through storage.read_video() — the bytes match.
    assert storage.read_video(videos[0].id) == _FAKE_MP4
    # Demo video is also appended to the default playlist.
    assert videos[0].id in playlist.load().item_ids


def test_seed_skips_demo_video_when_path_is_missing(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    """Bundled demo clip is optional — seeding still succeeds without it."""
    missing_path = tmp_path / "no-such-demo.mp4"

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=16,
        height=16,
        demo_video_path=missing_path,
    )
    # Only the gradient ImageSlides — no video.
    assert all(isinstance(s, ImageSlide) for s in created)


def test_seed_skips_demo_video_when_file_is_not_an_mp4(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    """Defense-in-depth: swapping a .mov or a PNG in doesn't crash seed."""
    bad_path = tmp_path / "demo.mp4"
    bad_path.write_bytes(b"\x89PNG\r\nnot an mp4 at all")

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=16,
        height=16,
        demo_video_path=bad_path,
    )
    assert all(isinstance(s, ImageSlide) for s in created)


def test_seed_demo_video_none_is_accepted(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """demo_video_path defaults to None; the no-demo-bundled path must work."""
    created = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert created
    assert all(isinstance(s, ImageSlide) for s in created)


def test_seed_skips_when_playlist_has_items_even_if_storage_is_empty(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """Weird state (content deleted but playlist intact) should NOT get
    seed items appended onto the pre-existing playlist."""
    from uuid import uuid4

    pl = playlist.load()
    pl.append(uuid4())
    playlist.save(pl)

    created = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert created == []
    # Marker recorded WHY it bailed.
    payload = json.loads(marker.read_text())
    assert payload["reason"] == "playlist-not-empty"
    # And the pre-existing playlist was untouched.
    assert len(playlist.load().item_ids) == 1
