"""FastAPI dependency providers.

Centralizes how API routes get their collaborators so tests can override them
via app.dependency_overrides and the production wiring stays in one place.
"""

import logging
import os
import socket
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

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


class AutoFallbackRenderer:
    """Wraps a primary RustRenderer; on `RustRendererSubprocessError`
    raised at any operation, transparently swaps to a MockRenderer for
    the rest of the session and logs the transition.

    Wraps the proxy AT THE FACTORY BOUNDARY -- the factory hands the
    playback loop an `AutoFallbackRenderer`, never a bare RustRenderer.
    Closes the loop on Phase 7 slice 2's robustness story: 1796584
    landed bounded auto-reconnect inside RustRenderer (3 retries in
    60s window). If reconnect exhausts, the proxy raises
    `RustRendererSubprocessError`. Without this wrapper, that would
    crash the playback loop. With it, prod degrades to MockRenderer
    (which writes PNGs to the dev preview path) and stays online.

    The swap is one-way and permanent: once Mock takes over, the dead
    proxy is torn down and never revived. Operators see a one-time
    log line + the renderer-monitor can detect the swap via
    `is_in_fallback`.

    Surface (matches the Renderer Protocol AT A MINIMUM):
      - `width: int` / `height: int` (forwarded to active renderer)
      - `render_frame(frame)` (the Protocol op; swap-on-error wired here)
      - Lifecycle: `open()` / `close()` / `__enter__` / `__exit__`
      - IPC ops forwarded to the proxy while alive: `begin_slide`,
        `advance`, `begin_transition`, `capture`, `reconfigure`.
        These all swap to Mock on `RustRendererSubprocessError`. Once
        in fallback, the IPC ops raise `AutoFallbackInMockError` --
        Mock can't satisfy IPC semantics so callers that depend on
        them (slice 4's playback bypass) need to know to switch
        to `render_frame`.
      - `RustRendererUnsupportedSlideError` from any IPC op (today:
        VideoSlide hitting paint_slide) is logged and re-raised
        unwrapped -- the proxy is fine, the SLIDE kind isn't
        supported. Playback loop catches the propagated exception
        and skips the slide (advance to next). DOES NOT swap to Mock.
      - `RustRendererUnsupportedTransitionError` from `begin_transition`
        is logged and re-raised unwrapped, parallel to the slide-kind
        case. Playback loop falls through to an instant cut. DOES NOT
        swap to Mock. Today this never fires (all 15 kinds are wired
        in Rust); forward-compat for an unknown kind.

    NOT forwarded: `is_alive`, `health_probe`, reconnect-related
    helpers -- these are RustRenderer-internal. The wrapper exposes
    its own `is_in_fallback` instead.
    """

    def __init__(
        self,
        primary: Any,  # RustRenderer; typed `Any` to avoid import cycle
        mock_factory: Callable[[], MockRenderer],
    ):
        self._primary = primary
        self._mock_factory = mock_factory
        self._mock: MockRenderer | None = None
        self._closed = False

    # --- inspection ---

    @property
    def is_in_fallback(self) -> bool:
        """True once the proxy was swapped out for Mock. One-way flag."""
        return self._mock is not None

    @property
    def width(self) -> int:
        return self._active_for_dims().width

    @property
    def height(self) -> int:
        return self._active_for_dims().height

    def _active_for_dims(self):
        if self._mock is not None:
            return self._mock
        return self._primary

    # --- the swap ---

    def _swap_to_mock(self, reason: str) -> MockRenderer:
        """Tear down the dead proxy and lazy-init the MockRenderer.
        Idempotent: a second call returns the existing Mock without
        re-tearing-down."""
        if self._mock is not None:
            return self._mock
        log.error("RustRenderer exhausted; falling back to MockRenderer: %s", reason)
        # Tear down the proxy BEFORE constructing Mock so any teardown
        # exception doesn't half-state us. Catch broad because the
        # proxy is by definition in a degraded state here.
        if self._primary is not None:
            try:
                self._primary.close()
            except Exception:
                log.debug("primary close during fallback swap raised", exc_info=True)
            self._primary = None
        self._mock = self._mock_factory()
        return self._mock

    # --- Renderer Protocol ---

    def render_frame(
        self,
        frame: bytes,
        *,
        pixel_format: str = "rgb888",
        frame_w: int | None = None,
        frame_h: int | None = None,
    ) -> None:
        """Forward to the active renderer. On subprocess exhaustion at
        the proxy, swap to Mock and replay the frame against it.

        HW-decode (2026-05-20): `pixel_format` + `frame_w`/`frame_h`
        are forwarded verbatim — the VLC NV12 pumps drive this through
        either the Rust proxy or the Mock fallback.

        IMPORTANT: `RustRendererRespawnedError` is a SUBCLASS of
        `RustRendererSubprocessError` (raised after a SUCCESSFUL
        reconnect — the proxy is alive, caller must replay state).
        We catch it FIRST and re-raise unwrapped so the playback loop
        can replay begin_slide rather than throwing away a healthy
        proxy.
        """
        if self._mock is not None:
            self._mock.render_frame(
                frame, pixel_format=pixel_format, frame_w=frame_w, frame_h=frame_h
            )
            return
        # Lazy import: avoids paying the rust_renderer module-import
        # cost when the factory routed elsewhere (auto/drm/mock paths).
        from openmarquee.rendering.rust_renderer import (
            RustRendererRespawnedError,
            RustRendererSubprocessError,
            RustRendererUnsupportedSlideError,
        )

        try:
            self._primary.render_frame(
                frame, pixel_format=pixel_format, frame_w=frame_w, frame_h=frame_h
            )
        except RustRendererRespawnedError:
            # Proxy is alive, just had a transient blip. Bubble up
            # so the caller knows to replay session state.
            raise
        except RustRendererUnsupportedSlideError:
            # render_frame doesn't normally raise this -- the proxy's
            # render_frame raises NotImplementedError per the Rust
            # sidecar contract (frames don't cross the IPC boundary).
            # But if a future impl ever did, treat the same as the
            # IPC-op path: don't swap to Mock, let caller handle.
            raise
        except RustRendererSubprocessError as e:
            mock = self._swap_to_mock(f"render_frame: {e}")
            mock.render_frame(frame, pixel_format=pixel_format, frame_w=frame_w, frame_h=frame_h)

    def end_external_frames(self) -> None:
        """Forward to the active renderer (STREAM/VLC slice 2.5).

        Mirrors render_frame's active-renderer selection. The VLC
        pumps call this in their finally after a run of render_frame()
        pushes; a subprocess death here swaps to Mock (consistent with
        render_frame's recovery), whose end_external_frames is a
        no-op.
        """
        if self._mock is not None:
            self._mock.end_external_frames()
            return
        from openmarquee.rendering.rust_renderer import (
            RustRendererRespawnedError,
            RustRendererSubprocessError,
        )

        try:
            self._primary.end_external_frames()
        except RustRendererRespawnedError:
            raise
        except RustRendererSubprocessError as e:
            mock = self._swap_to_mock(f"end_external_frames: {e}")
            mock.end_external_frames()

    def profile_start(self, frames: int) -> None:
        """Forward to the active RustRenderer proxy. After fallback
        swap there is no IPC sidecar to profile -- raise so the
        FastAPI handler (api_playback.perf_start) surfaces a clean
        503 instead of a misleading 204-success-with-no-data.

        perf-night r1 hotfix (2026-05-26): without this forwarder the
        endpoint's `getattr(renderer, "profile_start", None)` returned
        None -> 503 even when the primary RustRenderer was healthy
        (production AutoFallback wrap path).
        """
        if self._mock is not None:
            raise RuntimeError(
                "profile_start requires the RustRenderer IPC sidecar; "
                "AutoFallback has swapped to MockRenderer"
            )
        self._primary.profile_start(frames)

    def profile_dump(self) -> str:
        """Forward to the active RustRenderer proxy. Same fallback
        contract as profile_start."""
        if self._mock is not None:
            raise RuntimeError(
                "profile_dump requires the RustRenderer IPC sidecar; "
                "AutoFallback has swapped to MockRenderer"
            )
        return self._primary.profile_dump()

    def reopen(self) -> None:
        """Restart the active renderer so it picks up Open-time config
        (display rotation, FYS bug 5). Mock is a no-op; the
        RustRenderer proxy restarts its subprocess + re-Opens. A
        subprocess failure during the reopen swaps to Mock, consistent
        with the other forwarders. The caller (api_settings) stops the
        playback loop around this so nothing else drives the renderer.
        """
        if self._mock is not None:
            self._mock.reopen()
            return
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        try:
            self._primary.reopen()
        except RustRendererSubprocessError as e:
            self._swap_to_mock(f"reopen: {e}")

    # --- Lifecycle ---

    def open(self):
        """Forward open() to the proxy. If the proxy can't even open,
        swap to Mock immediately and re-raise so the lifespan sees the
        original failure cause. Subsequent `render_frame()` calls
        succeed against the Mock (lifespan can choose to log + continue
        rather than abort).

        Like `render_frame`, we catch `RustRendererRespawnedError` first
        and re-raise unwrapped — though it's unusual to see a respawn
        during the initial Open (it implies the very first launch's
        subprocess died and the proxy auto-reconnected).
        """
        if self._mock is not None:
            return None  # Mock has no open()
        from openmarquee.rendering.rust_renderer import (
            RustRendererRespawnedError,
            RustRendererSubprocessError,
        )

        try:
            return self._primary.open()
        except RustRendererRespawnedError:
            raise
        except RustRendererSubprocessError as e:
            self._swap_to_mock(f"open: {e}")
            raise

    def close(self) -> None:
        """Tear down whichever instance is active. Idempotent; safe
        to call multiple times. Mock has no close() of its own (it's
        a file-write target), so closing the wrapper after fallback
        just marks us closed."""
        if self._closed:
            return
        self._closed = True
        if self._primary is not None:
            try:
                self._primary.close()
            except Exception:
                log.debug("primary close during wrapper teardown raised", exc_info=True)
            self._primary = None
        # Drop the Mock reference too -- subsequent ops would fail
        # cleanly via the closed flag.
        self._mock = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self.close()
        except Exception:
            log.exception("AutoFallbackRenderer.close failed during __exit__")

    # --- IPC ops (forwarded to proxy; AutoFallbackInMockError after swap) ---

    def begin_slide(self, *args, **kwargs):
        return self._forward_ipc_op("begin_slide", args, kwargs)

    def advance(self, *args, **kwargs):
        return self._forward_ipc_op("advance", args, kwargs)

    def begin_transition(self, *args, **kwargs):
        return self._forward_ipc_op("begin_transition", args, kwargs)

    def capture(self, *args, **kwargs):
        return self._forward_ipc_op("capture", args, kwargs)

    def reconfigure(self, *args, **kwargs):
        return self._forward_ipc_op("reconfigure", args, kwargs)

    def preload_slide(self, *args, **kwargs):
        # r58 (2026-06-04, subagent BLOCKER fix): without this
        # forwarder, production calls to AutoFallbackRenderer.
        # preload_slide raise AttributeError -- caught by playback.py's
        # broad `except Exception` and logged as a non-fatal preload
        # failure once per slide-tail. Net effect: pre-warm never
        # reaches the sidecar in production AND the warning spams the
        # journal. Matches the begin_slide / advance / begin_transition
        # shape above so the AutoFallbackInMockError + subprocess-error
        # swap semantics apply uniformly.
        return self._forward_ipc_op("preload_slide", args, kwargs)

    def _forward_ipc_op(self, op_name: str, args: tuple, kwargs: dict):
        if self._mock is not None:
            raise AutoFallbackInMockError(
                f"{op_name}: wrapper is in MockRenderer fallback; IPC ops "
                f"unavailable. Switch to render_frame() for the rest of "
                f"the session, or restart the process to retry the sidecar."
            )
        from openmarquee.rendering.rust_renderer import (
            RustRendererRespawnedError,
            RustRendererSubprocessError,
            RustRendererUnsupportedSlideError,
            RustRendererUnsupportedTransitionError,
        )

        method = getattr(self._primary, op_name)
        try:
            return method(*args, **kwargs)
        except RustRendererRespawnedError:
            # Successful auto-reconnect; the proxy is alive. Let the
            # caller see the original RespawnedError so they replay
            # session state on the new subprocess.
            raise
        except RustRendererUnsupportedSlideError as e:
            # The slide kind isn't yet supported by the sidecar (today:
            # VideoSlide; task #76 wires V4L2). The proxy is fine --
            # we DO NOT swap to MockRenderer. Log + re-raise so the
            # playback loop can skip the slide and advance to the next.
            #
            # MUST come before the RustRendererSubprocessError clause
            # below; UnsupportedSlideError is a RustRendererOpError
            # subclass, not a SubprocessError subclass, so technically
            # it would propagate through the SubprocessError clause
            # without being caught -- but naming it explicitly here
            # both documents the policy and pins the log line.
            log.info(
                "AutoFallbackRenderer: %s skipped (unsupported slide kind): %s",
                op_name,
                e.message,
            )
            raise
        except RustRendererUnsupportedTransitionError as e:
            # Sidecar shader pipeline doesn't have this transition kind
            # wired; let the playback loop fall through to an instant
            # cut for this one transition. Forward-compat -- as of
            # 2026-05-14 every named kind is wired and the Rust side
            # silently FS_CUT-fallbacks for the unknown, so this never
            # fires today. Listed explicitly here so when Rust starts
            # emitting an explicit error for unknown kinds the policy
            # is already in place (same shape as UnsupportedSlideError:
            # log + re-raise, do NOT swap to Mock).
            log.info(
                "AutoFallbackRenderer: %s downgraded to cut (unsupported transition kind): %s",
                op_name,
                e.message,
            )
            raise
        except RustRendererSubprocessError as e:
            self._swap_to_mock(f"{op_name}: {e}")
            raise AutoFallbackInMockError(
                f"{op_name}: subprocess exhausted; swapped to MockRenderer. "
                f"Subsequent ops should use render_frame(). (cause: {e})"
            ) from e


class AutoFallbackInMockError(Exception):
    """Raised when an IPC op is called on an AutoFallbackRenderer that
    has already swapped to MockRenderer. Mock can't satisfy IPC
    semantics; callers must switch to `render_frame()` or restart the
    process to retry the sidecar.
    """


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

    output_mode=hdmi -> Rust IPC sidecar (RustRenderer wrapped in
    AutoFallbackRenderer). Anything else -> MockRenderer.

    Opt-in levers:
      OPENMARQUEE_RENDERER=mock          -> always mock (CI, dev)
      OPENMARQUEE_RENDERER=rust-sidecar  -> always rust sidecar (explicit)
      OPENMARQUEE_RENDERER=drm           -> legacy alias for rust-sidecar
        (kept for back-compat with operator scripts that still set drm)

    The renderer is constructed eagerly here but opened by app.py's
    lifespan so a __enter__ failure surfaces at startup, not first
    frame. lru_cache lifts construction out of the hot path; the
    singleton holds for the process lifetime.
    """
    override = os.environ.get("OPENMARQUEE_RENDERER", "auto").lower()
    if override == "mock":
        return _mock_renderer_singleton()
    if override in ("rust-sidecar", "drm"):
        return _rust_sidecar_renderer_or_fallback()

    settings = _settings_storage_singleton().load()
    if settings.output_mode == "hdmi":
        return _rust_sidecar_renderer_or_fallback()
    return _mock_renderer_singleton()


def _rust_sidecar_renderer_or_fallback():
    """Phase 7 slice 2 (2026-05-13) + slice 4-prep (2026-05-14):
    construct a RustRenderer instance from `rendering/rust_renderer.py`,
    wrapped in `AutoFallbackRenderer` so reconnect exhaustion at
    runtime degrades to MockRenderer instead of crashing playback.

    On IMPORT or CONSTRUCTION failure, fall back to bare
    MockRenderer immediately -- the AutoFallbackRenderer needs a
    primary to wrap, so no proxy means no wrapper.

    Env vars (proxy-specific):
      OPENMARQUEE_RENDERER_BINARY   path to the Rust binary
        (default: /usr/local/bin/openmarquee-render)
      OPENMARQUEE_CONTENT_ROOT      already used elsewhere; threaded
        through to the sidecar via the Open op's content_root param.

    Dims come from SystemSettings (display_width / display_height).
    The sidecar may negotiate a different mode on HDMI mode-set;
    RustRenderer.open() refreshes width/height to the negotiated
    values at __enter__ time.
    """
    try:
        from openmarquee.rendering.rust_renderer import RustRenderer
    except Exception:
        log.exception("rust_renderer module import failed; falling back to mock")
        return _mock_renderer_singleton()

    settings = _settings_storage_singleton().load()
    width = int(settings.display_width)
    height = int(settings.display_height)
    binary_path = os.environ.get("OPENMARQUEE_RENDERER_BINARY", "/usr/local/bin/openmarquee-render")
    content_root = _resolve_content_root()
    try:
        primary = RustRenderer(
            width=width,
            height=height,
            binary_path=binary_path,
            content_root=str(content_root),
            # Bug 1 follow-up (2026-05-20): the sidecar's auto_mode
            # clock renders local time; hand it the operator's
            # configured IANA tz so it sets TZ for libc localtime_r.
            # Callable (not a snapshot) so a respawn re-reads the
            # current setting.
            get_timezone=lambda: _settings_storage_singleton().load().timezone,
            # FYS bug 5: display rotation, read at each Open. A
            # rotation change triggers a renderer reopen
            # (api_settings.py), which re-reads this.
            get_rotation=lambda: int(_settings_storage_singleton().load().display_rotation),
        )
    except Exception:
        log.exception("RustRenderer construction failed; falling back to mock")
        return _mock_renderer_singleton()
    return AutoFallbackRenderer(primary, _mock_renderer_singleton)


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
    # Real renderer (Rust IPC sidecar on the device, mock in dev).
    # Selects on settings.output_mode + env override and falls back
    # to mock on init failure.
    renderer = _real_renderer_singleton()
    playlist_storage = _playlist_storage_singleton()
    schedule_storage = _schedule_storage_singleton()
    settings_storage = _settings_storage_singleton()

    # Closure-side loop reference so the fetch wrapper can stamp the
    # active playlist id back onto the loop (for the UI "now playing"
    # badge + the throttle-clear-on-playlist-change behavior). Kept
    # out of scheduled_fetch_items so that helper stays pure.
    loop_holder: dict = {}

    def fetch():
        active_id, items = scheduled_fetch_items(
            storage,
            playlist_storage,
            schedule_storage,
            datetime.now(),
        )
        loop = loop_holder.get("loop")
        if loop is not None:
            loop._stamp_playlist_id(active_id)
        return items

    def current_timezone() -> str | None:
        # Auto-mode slides render in this zone. Re-read each call so
        # changing the tz in Settings is reflected on the next tick.
        return settings_storage.load().timezone

    def active_playlist_id():
        # Bug 1 (2026-05-20): cheap "which playlist is active now"
        # probe the loop re-evaluates each slot so a schedule switch
        # preempts the running playlist. Just the schedule file read +
        # the pure schedule eval — no content/playlist load.
        from openmarquee.schedule import evaluate_schedule

        return evaluate_schedule(datetime.now(), schedule_storage.load())

    async def web_screenshot_producer(slide, width: int, height: int) -> bool:
        # Web slide: the screenshot-refresh producer the playback loop
        # fire-and-forgets when a Web slide's slot is stale. Renders
        # slide.url ON-DEVICE (headless Chromium) at the passed display
        # resolution (renderer.width/height — rotation-aware) and saves
        # the PNG to the slide's asset.png via save_web.
        # fetch_web_screenshot catches every failure itself — it never
        # raises out here.
        from openmarquee.web_screenshot import fetch_web_screenshot

        return await fetch_web_screenshot(slide, storage, width, height)

    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=fetch,
        read_asset=storage.read_asset,
        get_timezone=current_timezone,
        active_playlist_id=active_playlist_id,
        web_screenshot_producer=web_screenshot_producer,
    )
    loop_holder["loop"] = loop
    return loop


def get_playback_loop() -> PlaybackLoop:
    """Dependency provider for the playback engine."""
    return _playback_loop_singleton()


def get_web_screenshot_kicker() -> Callable[[Any], None]:
    """Dependency provider for the immediate Web-slide screenshot kick.

    Bug W1: a freshly-created (or url-changed) Web slide shows only the
    synthetic placeholder asset until its first playback slot — the
    dashboard thumbnail / editor preview / preview window are blank
    until then, because the screenshot producer is lazy/periodic.

    Returns a callable that, given the new/changed WebSlide, fires an
    IMMEDIATE fire-and-forget screenshot fetch via the playback loop's
    `kick_web_refresh_now`. The loop already owns the producer closure
    (with the helper URL/token + renderer dims) and the fire-and-forget
    + done-callback machinery — this provider just hands the API route
    a thin entry point onto it, so the route never blocks the request
    and never duplicates the producer's logic.

    Tests override this via `app.dependency_overrides` to assert the
    kick happens (or to stub it out) without spinning a real loop.
    """
    return _playback_loop_singleton().kick_web_refresh_now


@lru_cache
def _live_manager_singleton():
    """Process-wide live-takeover manager (SYSTEM_SPEC §5.11).

    Lazy-imported so a backend that never touches /api/live/* doesn't
    pay the aiortc import cost (it pulls cryptography, av, libvpx via
    PyAV — heavy). Holds at most one LiveSession at a time.
    """
    from openmarquee.live import LiveManager

    return LiveManager(_playback_loop_singleton())


def get_live_manager():
    """Dependency provider for the live-takeover manager."""
    return _live_manager_singleton()


# ============================================================
# P1.2-A (2026-06-10): network supervisor singleton.
# ============================================================
#
# The supervisor's NetworkSupervisor instance is process-wide. The
# observe-only asyncio loop (network_supervisor_loop.py) and the
# API endpoint (api_network_supervisor.py) share the same instance.
#
# Settings are read once at construction time; the fallback flag
# is currently NOT live-reloadable (set at boot from
# OPENMARQUEE_NETWORK_FALLBACK_MUTEX_MODE OR settings on first
# call). Live reload is a P3 concern when the operator-controllable
# "Setup Mode" UI lands.


@lru_cache
def _network_supervisor_singleton():
    from openmarquee.network_supervisor import NetworkSupervisor, SupervisorConfig
    from openmarquee.network_supervisor_actuator import (
        HostapdLifecycleActuator,
        WifiPowerSaveActuator,
    )

    storage = _settings_storage_singleton()
    settings = storage.load()
    config = SupervisorConfig(
        fallback_mutex_mode=settings.network_fallback_mutex_mode,
    )
    # P1.3 (2026-06-27): wire the netctl-driven power-save-off
    # actuator as the singleton's default. The actuator is fail-soft:
    # FileNotFoundError on dev hosts without the netctl socket is
    # caught by the supervisor's _fire_power_save_on_assoc and
    # downgraded to a warn diagnostic, so this is safe to install
    # unconditionally. Tests that construct NetworkSupervisor
    # directly (not via the singleton) get the observe-only stub.
    #
    # P2 (2026-06-27): same pattern for the AP lifecycle actuator.
    # The supervisor's _on_transition catches HostapdLifecycle-
    # ActuationError and downgrades to a warn diagnostic, so this
    # is safe to install unconditionally.
    return NetworkSupervisor(
        config=config,
        power_save_actuator=WifiPowerSaveActuator(),
        ap_lifecycle_actuator=HostapdLifecycleActuator(),
    )


def get_network_supervisor():
    """Dependency provider for the network supervisor.

    Returns the process-wide NetworkSupervisor instance. The supervisor
    starts in OBSERVE-ONLY mode (P1.2-A); the take-over commit
    (P1.2-B) flips the active actuator on.
    """
    return _network_supervisor_singleton()
