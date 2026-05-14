"""FastAPI dependency providers.

Centralizes how API routes get their collaborators so tests can override them
via app.dependency_overrides and the production wiring stays in one place.
"""

import logging
import os
import socket
import tempfile
from functools import lru_cache
from pathlib import Path

from openmarquee.content.storage import ContentStorage
from openmarquee.flock import FlockStorage
from openmarquee.flock_sync import FlockSync, PullWorker
from openmarquee.playback import PlaybackLoop
from openmarquee.playlist import PlaylistStorage
from openmarquee.rendering.mock import MockRenderer
from openmarquee.schedule import ScheduleStorage
from openmarquee.settings import SettingsStorage
from openmarquee.tombstone import TombstoneStorage

log = logging.getLogger(__name__)


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


def _resolve_dev_preview_path() -> Path:
    override = os.environ.get("OPENMARQUEE_DEV_PREVIEW_PATH")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "openmarquee-preview.png"


@lru_cache
def _real_renderer_singleton():
    """Pick + construct (but don't open) the production renderer based
    on settings.output_mode and OPENMARQUEE_RENDERER env override.

    output_mode=hdmi -> DRMRenderer (multi-plane KMS, vc4 HVS scanout).
    Anything else (or DRM init failure) -> MockRenderer.

    Opt-in levers:
      OPENMARQUEE_RENDERER=mock          -> always mock
      OPENMARQUEE_RENDERER=drm           -> always try DRM (override settings)
      OPENMARQUEE_RENDERER=rust-sidecar  -> Phase 7 slice 2: use the Rust
        IPC sidecar proxy (backend/openmarquee/rendering/rust_renderer.py).
        Construction-only here; the proxy launches the subprocess on
        open(). Binary path defaults to /usr/local/bin/openmarquee-render
        overridable via OPENMARQUEE_RENDERER_BINARY.

    The renderer is constructed eagerly here but opened by app.py's
    lifespan so a __enter__ failure surfaces at startup, not first
    frame. lru_cache lifts construction out of the hot path; the
    singleton holds for the process lifetime.
    """
    override = os.environ.get("OPENMARQUEE_RENDERER", "auto").lower()
    if override == "mock":
        return _mock_renderer_singleton()
    if override == "rust-sidecar":
        return _rust_sidecar_renderer_or_fallback()

    settings = _settings_storage_singleton().load()
    output_mode = settings.output_mode

    want_drm = override == "drm" or (override == "auto" and output_mode == "hdmi")
    if not want_drm:
        return _mock_renderer_singleton()

    try:
        from openmarquee.rendering.drm_kms import DRMRenderer
    except Exception:
        log.exception("DRM module import failed; falling back to mock")
        return _mock_renderer_singleton()

    width = int(settings.display_width)
    height = int(settings.display_height)
    # vc4 on Pi Zero 2 W has 3+ overlay planes; default 2 keeps GL
    # context overhead modest. Slides with more animated layers fall
    # back to compose_motion_frame software via the play loop's
    # gating in _play_dynamic_slide. Override via env on bigger Pis.
    max_animated_planes = int(
        os.environ.get("OPENMARQUEE_MAX_ANIMATED_PLANES", "2")
    )
    # Per-slide primary buffer pool size. Each entry is a kernel
    # dumb buffer at width*height*bytes_per_pixel (~4 MB at 1080p
    # RGB565). 20 was the original default but the 32-slide welcome
    # reel saturated all 20 entries in steady state -- 80 MB of
    # kernel memory left no room for ShaderRenderer's GBM/EGL
    # buffers and the system OOM'd uvicorn on a 416 MB Pi Zero 2 W
    # (caught 2026-05-06). 6 keeps recently-visited slides warm
    # (single-fb-flip attach) while bounding kernel memory at ~24 MB
    # so shader transitions fit. Raise on bigger Pis via env.
    max_pool_buffers = int(
        os.environ.get("OPENMARQUEE_MAX_POOL_BUFFERS", "6")
    )
    try:
        return DRMRenderer(
            width=width,
            height=height,
            display_width=width,
            display_height=height,
            max_animated_planes=max_animated_planes,
            max_pool_buffers=max_pool_buffers,
        )
    except Exception:
        log.exception("DRMRenderer construction failed; falling back to mock")
        return _mock_renderer_singleton()


def _rust_sidecar_renderer_or_fallback():
    """Phase 7 slice 2 (2026-05-13): construct a RustRenderer instance
    from `rendering/rust_renderer.py`. On import or construction failure,
    fall back to MockRenderer (same defensive pattern the DRM path uses).

    Env vars (proxy-specific):
      OPENMARQUEE_RENDERER_BINARY   path to the Rust binary
        (default: /usr/local/bin/openmarquee-render)
      OPENMARQUEE_CONTENT_ROOT      already used elsewhere; threaded
        through to the sidecar via the Open op's content_root param.

    Dims come from SystemSettings (display_width / display_height) just
    like the DRMRenderer path. The sidecar may negotiate a different mode
    on HDMI mode-set; RustRenderer.open() refreshes width/height to the
    negotiated values at __enter__ time.

    NOTE: this branch does NOT change the default. Production Pi behavior
    is unchanged until an operator sets OPENMARQUEE_RENDERER=rust-sidecar.
    Slice 4 will teach playback.py to use the proxy's IPC ops instead of
    render_frame; until then, even an opted-in caller will hit the
    NotImplementedError on the first frame (the proxy refuses push-frame
    rendering by design).
    """
    try:
        from openmarquee.rendering.rust_renderer import RustRenderer
    except Exception:
        log.exception("rust_renderer module import failed; falling back to mock")
        return _mock_renderer_singleton()

    settings = _settings_storage_singleton().load()
    width = int(settings.display_width)
    height = int(settings.display_height)
    binary_path = os.environ.get(
        "OPENMARQUEE_RENDERER_BINARY", "/usr/local/bin/openmarquee-render"
    )
    content_root = _resolve_content_root()
    try:
        return RustRenderer(
            width=width,
            height=height,
            binary_path=binary_path,
            content_root=str(content_root),
        )
    except Exception:
        log.exception("RustRenderer construction failed; falling back to mock")
        return _mock_renderer_singleton()


def get_renderer():
    """Production renderer dependency provider. The lifespan opens it
    at startup and closes it at shutdown."""
    return _real_renderer_singleton()


@lru_cache
def _mock_renderer_singleton() -> MockRenderer:
    """Build the dev MockRenderer with dims sourced from SystemSettings.

    The renderer re-reads settings on every frame, so changing
    display_width / display_height / display_rotation in the Settings
    UI flows through to the preview + the /simulator.html pop-out on
    the next tick — no backend restart needed. Portrait rotations
    (90°, 270°) swap the stored landscape-native dims so the dev
    preview's aspect ratio matches what an installed-rotated sign
    would show.

    Env-override is retained via OPENMARQUEE_DEV_WIDTH/HEIGHT — tests
    + CI pin a small canvas for speed, bypassing settings. When either
    override is present we use static dims; otherwise it's dynamic.
    """
    env_w = os.environ.get("OPENMARQUEE_DEV_WIDTH")
    env_h = os.environ.get("OPENMARQUEE_DEV_HEIGHT")
    if env_w or env_h:
        width = int(env_w or "128")
        height = int(env_h or "96")
        return MockRenderer(width, height, _resolve_dev_preview_path())

    settings_storage = _settings_storage_singleton()

    def current_dims() -> tuple[int, int]:
        s = settings_storage.load()
        if s.display_rotation in (90, 270):
            return (int(s.display_height), int(s.display_width))
        return (int(s.display_width), int(s.display_height))

    return MockRenderer(
        output_path=_resolve_dev_preview_path(),
        get_dims=current_dims,
    )


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
    # Pass the playlist storage in so the schedule can transparently
    # migrate v1 (playlist_name strings) → v2 (playlist_id UUIDs) by
    # resolving names against the playlist collection on first load.
    return ScheduleStorage(
        _resolve_schedule_path(),
        playlist_storage=_playlist_storage_singleton(),
    )


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


# --- Batch 20.1 / phase A.1: auth storage (password hash + token version) ---


def _resolve_auth_path() -> Path:
    """Where `auth.json` lives. Production (systemd unit) sets
    OPENMARQUEE_AUTH_PATH explicitly to /var/openmarquee/auth.json."""
    override = os.environ.get("OPENMARQUEE_AUTH_PATH")
    if override:
        return Path(override)
    return _resolve_content_root().parent / "openmarquee-auth.json"


@lru_cache
def _auth_storage_singleton():
    # Import here to avoid a circular dep at module-import time
    # (auth.py imports _atomic and _storage_recovery; both are
    # leaves, but keeping the lazy resolution clean avoids future
    # ordering surprises).
    from openmarquee.auth import AuthStorage
    return AuthStorage(_resolve_auth_path())


def get_auth_storage():
    """Dependency provider for the AuthStorage (password hash +
    token version)."""
    return _auth_storage_singleton()


def _resolve_flock_path() -> Path:
    """Where `flock.json` lives (peer list + per-peer sync flag). Sibling
    of the playlist/schedule/settings by default."""
    override = os.environ.get("OPENMARQUEE_FLOCK_PATH")
    if override:
        return Path(override)
    return _resolve_content_root().parent / "openmarquee-flock.json"


@lru_cache
def _flock_storage_singleton() -> FlockStorage:
    return FlockStorage(_resolve_flock_path())


def get_flock_storage() -> FlockStorage:
    """Dependency provider for the flock (peer-device list) storage layer."""
    return _flock_storage_singleton()


def _resolve_tombstone_path() -> Path:
    """Where `tombstones.json` lives (recently-deleted content_ids for
    flock-sync catch-up). Sibling of the other state files."""
    override = os.environ.get("OPENMARQUEE_TOMBSTONE_PATH")
    if override:
        return Path(override)
    return _resolve_content_root().parent / "openmarquee-tombstones.json"


@lru_cache
def _tombstone_storage_singleton() -> TombstoneStorage:
    return TombstoneStorage(_resolve_tombstone_path())


def get_tombstone_storage() -> TombstoneStorage:
    """Dependency provider for the tombstone log (deleted-content breadcrumbs)."""
    return _tombstone_storage_singleton()


def _resolve_self_address() -> str | None:
    """Where peers should reach THIS device. Returns None if no reachable
    form is known — push-notify skips with a warning rather than send
    pushes stamped with something peers can't reach.

    Priority:
      1. OPENMARQUEE_SELF_ADDRESS env override (tests, containers).
      2. SystemSettings.tailscale_hostname (the real production path).
      3. socket.gethostname() IFF it contains a dot (rejecting bare
         short names like "raspberrypi" that peers can't resolve).
    """
    override = os.environ.get("OPENMARQUEE_SELF_ADDRESS")
    if override:
        return override
    settings = _settings_storage_singleton().load()
    if settings.tailscale_hostname:
        return settings.tailscale_hostname
    try:
        hostname = socket.gethostname()
    except Exception:
        return None
    if "." in hostname:
        return hostname
    return None


def _resolve_flock_sync_enabled() -> bool:
    """Global kill switch — reads SystemSettings.flock_sync_enabled on every
    call so toggling it in the UI propagates to the next notify/pull tick
    without restarting the process."""
    return _settings_storage_singleton().load().flock_sync_enabled


@lru_cache
def _flock_sync_singleton() -> FlockSync:
    return FlockSync(
        content_storage=_content_storage_singleton(),
        tombstone_storage=_tombstone_storage_singleton(),
        flock_storage=_flock_storage_singleton(),
        get_self_address=_resolve_self_address,
        get_sync_enabled=_resolve_flock_sync_enabled,
    )


def get_flock_sync() -> FlockSync:
    """Dependency provider for the flock sync engine (push/pull orchestrator)."""
    return _flock_sync_singleton()


def _resolve_pull_interval_seconds() -> float:
    """Periodic pull cadence. Env override for tests; 60s default.
    Short enough that a dropped push is user-invisible within a minute."""
    override = os.environ.get("OPENMARQUEE_PULL_INTERVAL_SECONDS")
    if override:
        return float(override)
    return 60.0


@lru_cache
def _pull_worker_singleton() -> PullWorker:
    return PullWorker(
        sync=_flock_sync_singleton(),
        interval_seconds=_resolve_pull_interval_seconds(),
    )


def get_pull_worker() -> PullWorker:
    """Dependency provider for the periodic pull worker (reliability backstop)."""
    return _pull_worker_singleton()


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

    storage = _content_storage_singleton()
    # Real renderer (DRM/HDMI on the device, mock in dev). Was hardwired
    # to mock; now selects on settings.output_mode + env override and
    # falls back to mock on init failure.
    renderer = _real_renderer_singleton()
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

    def current_timezone() -> str | None:
        # Auto-mode slides render in this zone. Re-read each call so
        # changing the tz in Settings is reflected on the next tick.
        return settings_storage.load().timezone

    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=fetch,
        read_asset=storage.read_asset,
        get_timezone=current_timezone,
    )
    loop_holder["loop"] = loop
    return loop


def get_playback_loop() -> PlaybackLoop:
    """Dependency provider for the playback engine."""
    return _playback_loop_singleton()


@lru_cache
def _stream_manager_singleton():
    """Process-wide stream takeover manager (SYSTEM_SPEC §5.11).

    Lazy-imported so a backend that never touches /api/stream/* doesn't
    pay the aiortc import cost (it pulls cryptography, av, libvpx via
    PyAV — heavy). Holds at most one StreamSession at a time.
    """
    from openmarquee.stream import StreamManager

    return StreamManager(_playback_loop_singleton())


def get_stream_manager():
    """Dependency provider for the stream-takeover manager."""
    return _stream_manager_singleton()
