"""FastAPI dependency providers.

Centralizes how API routes get their collaborators so tests can override them
via app.dependency_overrides and the production wiring stays in one place.
"""

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from openmarquee.content.storage import ContentStorage
from openmarquee.playback import PlaybackLoop
from openmarquee.playlist import PlaylistStorage
from openmarquee.rendering.mock import MockRenderer
from openmarquee.schedule import ScheduleStorage
from openmarquee.settings import SettingsStorage


def _resolve_content_root() -> Path:
    """Pick a content root: env var override, then a sensible default.

    On the device the systemd unit will set OPENMARQUEE_CONTENT_ROOT to
    /var/openmarquee/content per SYSTEM_SPEC §3.3. For local dev we fall
    back to a relative ./openmarquee-content/ so running the app from
    anywhere gives a writable directory next to it.
    """
    override = os.environ.get("OPENMARQUEE_CONTENT_ROOT")
    if override:
        return Path(override)
    return Path("openmarquee-content").resolve()


@lru_cache
def _content_storage_singleton() -> ContentStorage:
    return ContentStorage(_resolve_content_root())


def get_content_storage() -> ContentStorage:
    """Dependency provider for the content storage layer."""
    return _content_storage_singleton()


def _resolve_dev_renderer_dimensions() -> tuple[int, int]:
    """Display dimensions for the dev MockRenderer. Defaults match SYSTEM_SPEC §3.4."""
    width = int(os.environ.get("OPENMARQUEE_DEV_WIDTH", "128"))
    height = int(os.environ.get("OPENMARQUEE_DEV_HEIGHT", "96"))
    return width, height


def _resolve_dev_preview_path() -> Path:
    override = os.environ.get("OPENMARQUEE_DEV_PREVIEW_PATH")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "openmarquee-preview.png"


@lru_cache
def _mock_renderer_singleton() -> MockRenderer:
    width, height = _resolve_dev_renderer_dimensions()
    return MockRenderer(width, height, _resolve_dev_preview_path())


def get_mock_renderer() -> MockRenderer:
    """Dependency provider for the dev-time MockRenderer.

    This is the renderer the /dev/preview page reads from. In production
    (Phases 6/8/10) the playback engine targets a real renderer instead.
    """
    return _mock_renderer_singleton()


def _resolve_playlist_path() -> Path:
    """Where the playlist JSON lives. Env override or content-root sibling."""
    override = os.environ.get("OPENMARQUEE_PLAYLIST_PATH")
    if override:
        return Path(override)
    # Sibling of the content root by default — same parent so backups grab both.
    return _resolve_content_root().parent / "openmarquee-playlist.json"


@lru_cache
def _playlist_storage_singleton() -> PlaylistStorage:
    return PlaylistStorage(_resolve_playlist_path())


def get_playlist_storage() -> PlaylistStorage:
    """Dependency provider for the playlist storage layer."""
    return _playlist_storage_singleton()


def _resolve_schedule_path() -> Path:
    """Where the schedule JSON lives. Sibling of the playlist by default."""
    override = os.environ.get("OPENMARQUEE_SCHEDULE_PATH")
    if override:
        return Path(override)
    return _resolve_content_root().parent / "openmarquee-schedules.json"


@lru_cache
def _schedule_storage_singleton() -> ScheduleStorage:
    return ScheduleStorage(_resolve_schedule_path())


def get_schedule_storage() -> ScheduleStorage:
    """Dependency provider for the schedule storage layer."""
    return _schedule_storage_singleton()


def _resolve_settings_path() -> Path:
    """Where `settings.json` lives. Sibling of the playlist/schedule by default.

    Production (systemd unit) sets OPENMARQUEE_SETTINGS_PATH explicitly.
    """
    override = os.environ.get("OPENMARQUEE_SETTINGS_PATH")
    if override:
        return Path(override)
    return _resolve_content_root().parent / "openmarquee-settings.json"


@lru_cache
def _settings_storage_singleton() -> SettingsStorage:
    return SettingsStorage(_resolve_settings_path())


def get_settings_storage() -> SettingsStorage:
    """Dependency provider for the system-settings storage layer."""
    return _settings_storage_singleton()


def _resolve_seed_marker_path() -> Path:
    """Where the first-boot seed marker lives. Sibling of content by default."""
    override = os.environ.get("OPENMARQUEE_SEED_MARKER_PATH")
    if override:
        return Path(override)
    return _resolve_content_root().parent / "openmarquee-seeded.json"


def get_seed_marker_path() -> Path:
    """Dependency provider for the seed-marker path."""
    return _resolve_seed_marker_path()


def _resolve_demo_video_path() -> Path:
    """Where first-boot seed looks for a bundled demo video.

    Not committed to git — the asset is provisioned out-of-band (see
    scripts/download-demo-video.sh) or baked into the pi-gen image.
    Default path sits next to the openmarquee package so the bundled
    asset travels with the Python install.
    """
    override = os.environ.get("OPENMARQUEE_DEMO_VIDEO_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "seed_assets" / "demo.mp4"


def get_demo_video_path() -> Path:
    """Dependency provider for the demo-video path (may not exist)."""
    return _resolve_demo_video_path()


@lru_cache
def _playback_loop_singleton() -> PlaybackLoop:
    from datetime import datetime

    from openmarquee.playback import scheduled_fetch_items
    from openmarquee.settings import pipeline_for_output_mode

    storage = _content_storage_singleton()
    renderer = _mock_renderer_singleton()
    playlist_storage = _playlist_storage_singleton()
    schedule_storage = _schedule_storage_singleton()
    settings_storage = _settings_storage_singleton()

    # Closure deferred so we can pass `loop` into the fetch fn for the
    # current-playlist-name stamp.
    loop_holder: dict = {}

    def fetch():
        return scheduled_fetch_items(
            storage,
            playlist_storage,
            schedule_storage,
            datetime.now(),
            loop=loop_holder.get("loop"),
        )

    def expected_pipeline() -> str | None:
        # Loaded on every iteration so a live-on-device settings change
        # (operator switches hdmi → hub75 in the UI) takes effect on the
        # very next slide — no loop restart needed.
        return pipeline_for_output_mode(settings_storage.load().output_mode)

    def current_timezone() -> str | None:
        # Auto-mode slides render in this zone. Re-read each call so
        # changing the tz in Settings is reflected on the next tick.
        return settings_storage.load().timezone

    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=fetch,
        read_asset=storage.read_asset,
        get_expected_video_pipeline=expected_pipeline,
        get_timezone=current_timezone,
    )
    loop_holder["loop"] = loop
    return loop


def get_playback_loop() -> PlaybackLoop:
    """Dependency provider for the playback engine."""
    return _playback_loop_singleton()
