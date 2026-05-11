"""Unit tests for first-boot content seeding."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image  # noqa: F401  (used via fixture _write_sample_jpeg)

from openmarquee.content import TextSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.playlist import PlaylistStorage
from openmarquee.seed import (
    SEED_VERSION,
    SeedPreset,
    render_gradient_png,
    render_welcome_png,
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


@pytest.fixture
def empty_bundled_dir(tmp_path: Path) -> Path:
    """An empty directory to pin the fallback-gradients seed path in tests
    that aren't specifically about the bundled-backgrounds branch."""
    d = tmp_path / "bundled-empty"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def no_bundled_assets_by_default(tmp_path: Path, monkeypatch):
    """Hide the repo's committed seed_assets/{backgrounds,videos}/ from
    every test by default — otherwise the starter-slide tests would see
    the curated pack rather than the Pillow-generated fallback they're
    written for. Tests that want to exercise the bundled paths pass an
    explicit `bundled_backgrounds_dir=` / `bundled_videos_dir=` to
    seed_if_needed."""
    empty_bg = tmp_path / "auto-empty-backgrounds"
    empty_bg.mkdir()
    empty_vid = tmp_path / "auto-empty-videos"
    empty_vid.mkdir()
    monkeypatch.setattr(
        "openmarquee.seed._default_bundled_backgrounds_dir", lambda: empty_bg
    )
    monkeypatch.setattr(
        "openmarquee.seed._default_bundled_videos_dir", lambda: empty_vid
    )


def _write_sample_jpeg(path: Path, color=(100, 100, 100), size=(512, 512)) -> None:
    """Write a small-but-real JPEG to `path` for bundled-background tests."""
    img = Image.new("RGB", size, color)
    img.save(path, format="JPEG")


# --- render_gradient_png ---


def test_render_gradient_png_returns_valid_png_at_requested_size():
    preset = SeedPreset(name="x", top=(10, 20, 30), bottom=(200, 100, 50))
    png = render_gradient_png(preset, 64, 48)
    img = Image.open(io.BytesIO(png))
    img.verify()
    img = Image.open(io.BytesIO(png))  # re-open: verify() exhausts the stream
    assert img.size == (64, 48)
    assert img.mode == "RGB"


def test_welcome_png_is_valid_and_at_requested_dimensions():
    png = render_welcome_png(128, 96)
    img = Image.open(io.BytesIO(png))
    img.verify()
    img = Image.open(io.BytesIO(png))
    assert img.size == (128, 96)
    assert img.mode == "RGB"


def test_text_slide_renders_inside_box(tmp_path):
    """qarl 2026-04-30 §5.10a: text renders inside the box, not centered
    on the slide. With a half-width box on the right, the text-bearing
    pixels should sit on the right half of the canvas — the left half
    is just background."""
    from openmarquee.content import TextBox
    from openmarquee.seed import render_text_slide_png

    box = TextBox(x=0.5, y=0.1, w=0.4, h=0.4)  # top-right quadrant
    png = render_text_slide_png(
        "X",
        100,
        100,
        fg="#FFFFFF",
        bg="#000000",
        box=box,
    )
    img = Image.open(io.BytesIO(png)).convert("RGB")
    pixels = img.load()
    top_right = top_left = bottom_left = bottom_right = 0
    for y in range(100):
        for x in range(100):
            if pixels[x, y] == (0, 0, 0):
                continue
            if x >= 50 and y < 50:
                top_right += 1
            elif x < 50 and y < 50:
                top_left += 1
            elif x < 50 and y >= 50:
                bottom_left += 1
            else:
                bottom_right += 1
    assert top_right > top_left + bottom_left + bottom_right, (
        f"text bled out of box: tr={top_right} tl={top_left} "
        f"bl={bottom_left} br={bottom_right}"
    )


def test_text_slide_squishes_long_text_horizontally(tmp_path):
    """B7 (qarl batch 2026-04-29): long text should squish horizontally
    to fit, mirroring the UI editor's `fillText(maxWidth)` treatment.
    Previously the renderer would shrink the font until the text fit;
    now the font stays at the height-anchored size and the rendered
    glyphs scale horizontally onto the canvas."""
    from openmarquee.seed import render_text_slide_png

    # 32-wide canvas; "openMarquee" is well over the natural width at
    # the height-derived font size, so the squish path is exercised.
    png = render_text_slide_png("openMarquee", 32, 32)
    img = Image.open(io.BytesIO(png))
    img.verify()
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.size == (32, 32)
    # Smoke check: not entirely the background — text actually rendered.
    pixels = img.getdata()
    distinct = set(pixels)
    assert len(distinct) > 1


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

    # At least the 4 fallback backgrounds + 3 welcome text slides + 3
    # Freedom text slides (FREE / YOUR / SIGN).
    assert len(created) >= 10

    # They round-trip through storage.
    loaded = storage.list_all()
    assert len(loaded) == len(created)

    # Mix of TextSlide (welcome + freedom slides) + ImageSlide (backgrounds).
    types = sorted({s.type for s in created})
    assert "image" in types
    assert "text_slide" in types


def test_seed_default_playlist_contains_the_three_welcome_text_slides(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """qarl's requirement: fresh boot → default (Welcome) playlist holds
    the three intro text slides ('Welcome' / 'to' / 'openMarquee') in
    order. Backgrounds + videos are available content but aren't in the
    playlist — the operator drags what they want onto the track."""
    created = seed_if_needed(storage, playlist, marker, width=32, height=32)

    # Welcome text slides come first in the seed flow; Freedom slides
    # (FREE / YOUR / SIGN) come after, on a separate playlist.
    text_slides = [s for s in created if s.type == "text_slide"]
    welcome_slides = text_slides[:3]
    assert [s.text_layers[0].text for s in welcome_slides] == ["Welcome", "to", "openMarquee"]

    # Default playlist holds exactly those three, in the same order.
    ids = playlist.load().item_ids
    assert ids == [s.id for s in welcome_slides]


def test_welcome_slides_render_with_qarl_specified_fonts(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """qarl batch 2026-04-29 (B8/B9/B10): Welcome → Reenie Beanie,
    to → Permanent Marker, openMarquee → Shadows Into Light. Locks the
    pairing so a refactor of the spec list doesn't silently revert the
    chosen pairings."""
    created = seed_if_needed(storage, playlist, marker, width=32, height=32)
    text_slides = [s for s in created if s.type == "text_slide"]
    welcome = {s.text_layers[0].text: s for s in text_slides[:3]}
    assert welcome["Welcome"].text_layers[0].font_family == "Reenie Beanie"
    assert welcome["to"].text_layers[0].font_family == "Permanent Marker"
    assert welcome["openMarquee"].text_layers[0].font_family == "Shadows Into Light"


def test_seed_default_playlist_is_named_welcome(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """qarl's 2026-04-28 ask: the default playlist's display name is
    'Welcome' (matching its solo seeded content), not the legacy
    'default'. Identity is still the stable DEFAULT_PLAYLIST_ID UUID,
    so schedule rules + API references survive the rename."""
    seed_if_needed(storage, playlist, marker, width=32, height=32)

    from openmarquee.playlist import DEFAULT_PLAYLIST_ID

    default_pl = playlist.get_by_id(DEFAULT_PLAYLIST_ID)
    assert default_pl is not None
    assert default_pl.name == "Welcome"


def test_seed_creates_freedom_playlist_with_three_slides(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """qarl's 2026-04-28 ask: fresh boot also creates a 'Freedom'
    playlist with three protest-poster slides reading FREE / YOUR /
    SIGN. Played by the Friday-night schedule rule."""
    created = seed_if_needed(storage, playlist, marker, width=32, height=32)

    text_slides = [s for s in created if s.type == "text_slide"]
    freedom_slides = text_slides[3:6]
    assert [s.text_layers[0].text for s in freedom_slides] == ["FREE", "YOUR", "SIGN"]

    # Freedom playlist exists alongside the default (Welcome) playlist.
    collection = playlist.load_all()
    by_name = {p.name: p for p in collection.playlists}
    assert "Freedom" in by_name
    freedom_pl = by_name["Freedom"]
    assert [item.item_id for item in freedom_pl.items] == [
        s.id for s in freedom_slides
    ]


def test_seed_writes_friday_2000_freedom_rule_into_schedule(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    """qarl's 2026-04-28 ask: schedule has a Friday 20:00-20:10 rule
    pointing at the Freedom playlist; default fallback stays on the
    Welcome (default) playlist for all other times."""
    from openmarquee.playlist import DEFAULT_PLAYLIST_ID
    from openmarquee.schedule import ScheduleStorage

    schedule_storage = ScheduleStorage(
        tmp_path / "schedule.json",
        playlist_storage=playlist,
    )
    seed_if_needed(
        storage,
        playlist,
        marker,
        width=32,
        height=32,
        schedule_storage=schedule_storage,
    )

    schedule = schedule_storage.load()
    assert schedule.default_playlist_id == DEFAULT_PLAYLIST_ID
    assert len(schedule.rules) == 1
    rule = schedule.rules[0]
    assert rule.days == ["fri"]
    assert rule.start_time == "20:00"
    assert rule.end_time == "20:10"
    assert rule.enabled

    # Rule's playlist_id resolves to the Freedom playlist by name.
    collection = playlist.load_all()
    freedom = next(p for p in collection.playlists if p.name == "Freedom")
    assert rule.playlist_id == freedom.id


def test_seed_skips_schedule_rule_when_no_schedule_storage_provided(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """schedule_storage is optional — tests that don't care about the
    schedule rule should still see Freedom + Welcome land cleanly."""
    seed_if_needed(storage, playlist, marker, width=32, height=32)
    # No exception raised. Freedom playlist still landed.
    collection = playlist.load_all()
    assert any(p.name == "Freedom" for p in collection.playlists)


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


# --- bundled backgrounds ---


def test_seed_registers_bundled_backgrounds_over_pillow_fallback(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    bundled = tmp_path / "backgrounds"
    bundled.mkdir()
    _write_sample_jpeg(bundled / "parchment.jpg", color=(230, 220, 180))
    _write_sample_jpeg(bundled / "midnight.jpg", color=(10, 20, 60))

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=32,
        height=32,
        bundled_backgrounds_dir=bundled,
    )
    # 2 bundled backgrounds + 3 welcome text slides + 3 freedom text
    # slides AT MINIMUM. No Pillow-gradient fallback when bundled
    # backgrounds are present. (10.fix: relaxed `== 8` to `>=` so
    # future seed expansion -- demo slides, motion / blend / pattern
    # samples -- doesn't trip the test. The bg_names + welcome
    # assertions are the load-bearing claims.)
    assert len(created) >= 8
    bg_names = sorted(
        s.name for s in created if s.name.endswith("— Background")
    )
    assert bg_names == ["Midnight — Background", "Parchment — Background"]
    # The default playlist holds exactly the three Welcome text slides;
    # Freedom slides land on a separate playlist (covered elsewhere).
    text_slides = [s for s in created if s.type == "text_slide"]
    welcome_slides = text_slides[:3]
    assert [s.text_layers[0].text for s in welcome_slides] == ["Welcome", "to", "openMarquee"]
    assert playlist.load().item_ids == [s.id for s in welcome_slides]


def test_seed_falls_back_to_gradients_when_bundled_dir_is_empty(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    empty_bundled_dir: Path,
):
    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=32,
        height=32,
        bundled_backgrounds_dir=empty_bundled_dir,
    )
    # 4 Pillow-gradient presets + 3 welcome slides + 3 freedom
    # slides AT MINIMUM. (10.fix: relaxed `== 10` to `>=` so future
    # seed expansion stays compatible; the `len(bg_names) == 4`
    # assertion below is the load-bearing claim for this test.)
    assert len(created) >= 10
    bg_names = [s.name for s in created if s.name.endswith("— Background")]
    assert len(bg_names) == 4


def test_seed_bundled_skips_unreadable_files_but_still_seeds_good_ones(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    bundled = tmp_path / "backgrounds"
    bundled.mkdir()
    _write_sample_jpeg(bundled / "good.jpg")
    (bundled / "broken.jpg").write_bytes(b"not an image at all")

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=32,
        height=32,
        bundled_backgrounds_dir=bundled,
    )
    # Good bundled + 3 welcome + 3 freedom slides = 7 items; broken file
    # logged + skipped.
    names = sorted(s.name for s in created)
    assert names == [
        "FREE",
        "Good — Background",
        "SIGN",
        "Welcome",
        "YOUR",
        "openMarquee",
        "to",
    ]


def test_seed_bundled_is_deterministic_across_runs(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    """Order should be filename-sorted so two fresh devices ship the same
    set of bundled backgrounds in the same order — welcome slides always
    last, in playlist order."""
    bundled = tmp_path / "backgrounds"
    bundled.mkdir()
    for stem in ["zebra", "alpha", "mango"]:
        _write_sample_jpeg(bundled / f"{stem}.jpg")

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=16,
        height=16,
        bundled_backgrounds_dir=bundled,
    )
    assert [s.name for s in created] == [
        "Alpha — Background",
        "Mango — Background",
        "Zebra — Background",
        "Welcome",
        "to",
        "openMarquee",
        "FREE",
        "YOUR",
        "SIGN",
    ]


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
    # Demo video is NOT auto-appended to the default (Welcome) playlist
    # — only the three welcome text slides are. Operator drags the demo
    # into the playlist themselves when they want to show it.
    assert videos[0].id not in playlist.load().item_ids
    text_slides = [s for s in created if s.type == "text_slide"]
    welcome_slides = text_slides[:3]
    assert playlist.load().item_ids == [s.id for s in welcome_slides]


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
    # Gradient backgrounds + Welcome, but no video.
    assert not any(isinstance(s, VideoSlide) for s in created)


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
    assert not any(isinstance(s, VideoSlide) for s in created)


def test_seed_demo_video_none_is_accepted(
    storage: ContentStorage, playlist: PlaylistStorage, marker: Path
):
    """demo_video_path defaults to None; the no-demo-bundled path must work."""
    created = seed_if_needed(storage, playlist, marker, width=16, height=16)
    assert created
    assert not any(isinstance(s, VideoSlide) for s in created)


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


# --- bundled videos ---


def test_seed_bundled_videos_derives_title_from_filename(
    storage: ContentStorage,
    playlist: PlaylistStorage,
    marker: Path,
    tmp_path: Path,
):
    """Regression for the rename: sale.mp4 should seed as a VideoSlide
    named 'Sale' (and there must NOT be any 'Up To 70 Off' slide any
    longer, since the file was renamed from up-to-70-off.mp4)."""
    bundled = tmp_path / "videos"
    bundled.mkdir()
    # Pair: {name}.mp4 + {name}.png thumbnail.
    (bundled / "sale.mp4").write_bytes(_FAKE_MP4)
    _write_sample_jpeg(bundled / "sale.png")

    created = seed_if_needed(
        storage,
        playlist,
        marker,
        width=16,
        height=16,
        bundled_videos_dir=bundled,
    )

    video_slides = [s for s in created if isinstance(s, VideoSlide)]
    names = sorted(s.name for s in video_slides)
    assert names == ["Sale"]
    assert "Up To 70 Off" not in names


# --- Batch 9.fix: _write_marker atomic-write rollback ---


def test_write_marker_rollback_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """_write_marker uses the same tmp.write_text + tmp.replace
    atomic pattern as the 6 storage classes covered in Batch 9.2.
    When replace() raises, the orphan .tmp must be cleaned up so a
    retry doesn't fight a stale file. Same shape as
    test_storage_atomic_writes."""
    from openmarquee.seed import _write_marker

    marker_path = tmp_path / "seed-marker.json"
    # First write succeeds; baseline.
    _write_marker(marker_path, created=2, reason="initial")
    assert marker_path.exists()

    # Monkeypatch Path.replace to raise OSError on .tmp paths.
    original_replace = Path.replace

    def _raise_if_tmp(self: Path, target):
        if self.name.endswith(".tmp"):
            raise OSError(28, "simulated disk full")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _raise_if_tmp)

    with pytest.raises(OSError):
        _write_marker(marker_path, created=5, reason="retry")

    # Original marker intact (replace never landed).
    assert marker_path.exists()
    # No orphan .tmp left behind.
    assert not any(p.name.endswith(".tmp") for p in tmp_path.rglob("*"))
