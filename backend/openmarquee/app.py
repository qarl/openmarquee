"""FastAPI application that runs on the device."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from openmarquee import __version__
from openmarquee.api import router as content_router
from openmarquee.api_auth import router as auth_router
from openmarquee.api_backgrounds import router as backgrounds_router
from openmarquee.api_flock import router as flock_router
from openmarquee.api_live import router as live_router
from openmarquee.api_playback import router as playback_router
from openmarquee.api_playlist import router as playlist_router
from openmarquee.api_schedule import router as schedule_router
from openmarquee.api_settings import router as settings_router
from openmarquee.api_system import router as system_router
from openmarquee.dependencies import (
    get_auth_storage,
    get_content_storage,
    get_demo_video_path,
    get_live_manager,
    get_playback_loop,
    get_playlist_storage,
    get_pull_worker,
    get_renderer,
    get_schedule_storage,
    get_seed_marker_path,
    get_settings_storage,
)
from openmarquee.dev import router as dev_router
from openmarquee.perf_middleware import PerfMiddleware
from openmarquee.seed import seed_if_needed

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Set the root logger format + level for the device process.

    16.2 / sweep #8 A1: replace the bare uvicorn default (no
    timestamp, no module name, opaque "INFO:") with a timestamped
    structured-ish line that survives journalctl tailing. Level
    comes from OPENMARQUEE_LOG_LEVEL env (default INFO) so an
    operator can flip to DEBUG without code edit. Noisy third-party
    libs get explicitly silenced -- aiortc + httpx are chattiest
    at INFO and aren't useful for openMarquee-side debugging.
    """
    level_name = os.environ.get("OPENMARQUEE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
        level=level,
        # If a previous uvicorn entry installed handlers (it does),
        # force=True replaces them so our format wins.
        force=True,
    )
    # 16.3 / sweep #8 A4: tag every log record with the per-request
    # correlation id (or "-" when emitted outside a request, set by
    # the ContextVar's default). Attach the filter to the root
    # logger's handlers so all subloggers inherit it. Order matters:
    # the filter has to be installed BEFORE any log call runs against
    # the new format, otherwise the formatter would raise KeyError
    # on the missing request_id attribute.
    from openmarquee.perf_middleware import RequestIdLogFilter
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestIdLogFilter())
    # Silence chatty deps at INFO; their WARNING+ still surfaces.
    for noisy in ("aiortc", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: first-boot seed + auto-start playback. Shutdown: stop."""
    # First-boot seed: if no marker file + storage is empty, create a few
    # starter ImageSlides so the operator has something to hit Play on
    # immediately. seed_if_needed logs + stamps a marker so this is a
    # no-op on every boot after the first. Tests pin
    # OPENMARQUEE_DISABLE_SEED=1 to keep the lifespan path from populating
    # a test-fixture's tmp_path with surprise content.
    if os.environ.get("OPENMARQUEE_DISABLE_SEED") != "1":
        try:
            settings = get_settings_storage().load()
            seed_if_needed(
                storage=get_content_storage(),
                playlist_storage=get_playlist_storage(),
                marker_path=get_seed_marker_path(),
                width=settings.display_width,
                height=settings.display_height,
                demo_video_path=get_demo_video_path(),
                schedule_storage=get_schedule_storage(),
            )
        except Exception:
            # Seeding is nice-to-have; never block startup on it.
            log.exception("startup seed failed")

    # Prune playlist refs that no longer resolve to stored content. Catches
    # the "dev-wiped content/ but left playlist.json intact" class of bug
    # before the pallet renders dangling tiles or the playback loop hits
    # a FileNotFoundError mid-slide.
    try:
        valid_ids = {item.id for item in get_content_storage().list_all()}
        pruned = get_playlist_storage().prune_dangling_refs(valid_ids)
        if pruned:
            log.warning("startup: pruned %d dangling playlist item_id(s)", pruned)
    except Exception:
        log.exception("startup playlist prune failed")

    # Open the production renderer if it's a context manager (RustRenderer
    # spawns the openmarquee-render subprocess, negotiates the HDMI mode,
    # and opens the IPC handshake here -- failure should surface at
    # startup, not on the first render_frame call). MockRenderer is a
    # plain class with no __enter__; check before calling. On failure
    # we fall back to mock so the service still serves the UI even with
    # a misconfigured display.
    renderer = get_renderer()
    if hasattr(renderer, "__enter__"):
        try:
            renderer.__enter__()
        except Exception:
            log.exception("startup: renderer __enter__ failed; degrading to mock")
            # Force the singleton to swap to mock for the rest of the
            # process lifetime so the playback loop has a working target.
            # The next get_playback_loop() resolution will go through
            # _real_renderer_singleton again, see env=mock, and bind
            # the existing _mock_renderer_singleton.
            from openmarquee.dependencies import (
                _playback_loop_singleton,
                _real_renderer_singleton,
            )
            _real_renderer_singleton.cache_clear()
            _playback_loop_singleton.cache_clear()
            os.environ["OPENMARQUEE_RENDERER"] = "mock"

    # "Hardware always running" — the device's real playback loop starts
    # at boot and runs until shutdown. The UI's inline preview is a
    # parallel client-side simulator, not a control surface for the loop.
    # Tests can opt out via OPENMARQUEE_DISABLE_AUTOSTART=1 so fixtures
    # that spin a backend don't have an extra asyncio task running.
    if os.environ.get("OPENMARQUEE_DISABLE_AUTOSTART") != "1":
        try:
            await get_playback_loop().start()
        except Exception:
            log.exception("startup playback autostart failed")

    # Flock pull worker — periodic reliability backstop that reconciles
    # against sync=True peers even when pushes get dropped. Tests can
    # opt out via OPENMARQUEE_DISABLE_PULL_WORKER=1 so fixtures that
    # spin a backend don't have an extra asyncio task racing the assertions.
    if os.environ.get("OPENMARQUEE_DISABLE_PULL_WORKER") != "1":
        try:
            await get_pull_worker().start()
        except Exception:
            log.exception("startup pull worker autostart failed")
    yield
    # Tear down any active live session BEFORE the playback loop stops
    # so the session's close() can resume() the loop cleanly even though
    # the loop is about to exit. Order matters less now that resume() is
    # a no-op against a stopped loop, but the explicit shutdown order is
    # cheaper than reasoning about the race later.
    with suppress(Exception):
        await get_live_manager().stop_all()
    await get_playback_loop().stop()
    with suppress(Exception):
        await get_pull_worker().stop()
    # Close the renderer last -- after the playback loop has stopped
    # writing frames -- so we don't free its buffers mid-render.
    renderer = get_renderer()
    if hasattr(renderer, "__exit__"):
        with suppress(Exception):
            renderer.__exit__(None, None, None)


app = FastAPI(title="openMarquee", version=__version__, lifespan=lifespan)

# Perf middleware -- timestamps each HTTP request, logs slow ones,
# and pushes records into the in-memory ring exposed at
# /api/system/perf-stats. Mount BEFORE routers so it wraps them.
app.add_middleware(PerfMiddleware)

# Batch 20.1 / phase A.1: bearer-token gate. add_middleware stacks
# outer-most-first, so PerfMiddleware (added first) wraps
# AuthMiddleware -- perf records still cover auth-rejected 401s
# (useful for "are we 401-ing a lot?" observability). AuthMiddleware
# fails closed: requests not on the whitelist need a valid token.
from openmarquee.auth_middleware import AuthMiddleware

# Pass a callable resolver -- the middleware looks up the storage
# per-request so the singleton's lru_cache can be cleared between
# tests (tests point OPENMARQUEE_AUTH_PATH at a tmp dir).
app.add_middleware(AuthMiddleware, auth_storage_resolver=get_auth_storage)


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Override FastAPI's default 422 handler to drop the echoed input.

    FastAPI's default emits `detail = jsonable_encoder(exc.errors())`,
    which includes a verbatim copy of every offending field's value.
    For an over-cap prompt at /api/backgrounds/generate that's a
    multi-KB response body for what's morally "input too long" (QA
    explore-bg-gen 2026-04-26 → verify-d9d6efb-bg-gen-cap.md). Mirrors
    the api.py::_validation_error_422 helper's `include_input=False`
    behavior, applied uniformly to every endpoint's request-body
    validation.

    FastAPI's `RequestValidationError` subclasses pydantic's
    `ValidationError` but overrides `.errors()` to a Starlette-style
    list-of-dicts that doesn't accept `include_input=False` (unlike
    raw pydantic). So we filter the `input` key out manually and pass
    through `jsonable_encoder` for the same JSON-safety guarantees
    the default handler gives (UUIDs in `input` paths, exceptions in
    `ctx`).
    """
    sanitised = [{k: v for k, v in err.items() if k != "input"} for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(sanitised)},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Used by the dev script and (later) by systemd."""
    return {"status": "alive", "version": __version__}


app.include_router(auth_router)
app.include_router(content_router)
app.include_router(playlist_router)
app.include_router(schedule_router)
app.include_router(settings_router)
app.include_router(backgrounds_router)
app.include_router(playback_router)
app.include_router(live_router)
app.include_router(system_router)
app.include_router(flock_router)

# Dev tooling (preview page, manual play endpoint) is mounted by default
# because the device is its own captive-portal AP with no inbound internet.
# The SD-card image builder (Phase 9) sets OPENMARQUEE_DISABLE_DEV=1 to
# strip it from production images.
if os.environ.get("OPENMARQUEE_DISABLE_DEV") != "1":
    app.include_router(dev_router)


def _resolve_ui_dir() -> Path:
    """Where the UI static files live. backend/openmarquee/app.py → ../../ui/.

    TODO(phase-5/packaging): this path only resolves in a checkout.
    Once we `pip install` this package onto the Pi the sibling `ui/`
    won't exist in site-packages. Ship `ui/dist/` + `index.html` as
    package data and resolve via `importlib.resources`, or bake the
    path into the systemd unit's OPENMARQUEE_UI_DIR.
    """
    override = os.environ.get("OPENMARQUEE_UI_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "ui"


# UI mount goes LAST so registered routes (/healthz, /api/*, /dev/*) take
# precedence over the static fallback. `html=True` makes "/" serve index.html.
app.mount("/", StaticFiles(directory=_resolve_ui_dir(), html=True), name="ui")
