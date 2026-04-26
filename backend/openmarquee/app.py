"""FastAPI application that runs on the device."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from openmarquee import __version__
from openmarquee.api import router as content_router
from openmarquee.api_backgrounds import router as backgrounds_router
from openmarquee.api_flock import router as flock_router
from openmarquee.api_playback import router as playback_router
from openmarquee.api_system import router as system_router
from openmarquee.api_playlist import router as playlist_router
from openmarquee.api_schedule import router as schedule_router
from openmarquee.api_settings import router as settings_router
from openmarquee.dependencies import (
    get_content_storage,
    get_demo_video_path,
    get_playback_loop,
    get_playlist_storage,
    get_pull_worker,
    get_seed_marker_path,
    get_settings_storage,
)
from openmarquee.dev import router as dev_router
from openmarquee.seed import seed_if_needed


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
            )
        except Exception:
            # Seeding is nice-to-have; never block startup on it.
            import logging

            logging.getLogger(__name__).exception("startup seed failed")

    # Prune playlist refs that no longer resolve to stored content. Catches
    # the "dev-wiped content/ but left playlist.json intact" class of bug
    # before the pallet renders dangling tiles or the playback loop hits
    # a FileNotFoundError mid-slide.
    try:
        valid_ids = {item.id for item in get_content_storage().list_all()}
        pruned = get_playlist_storage().prune_dangling_refs(valid_ids)
        if pruned:
            import logging
            logging.getLogger(__name__).warning(
                "startup: pruned %d dangling playlist item_id(s)", pruned
            )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("startup playlist prune failed")

    # "Hardware always running" — the device's real playback loop starts
    # at boot and runs until shutdown. The UI's inline preview is a
    # parallel client-side simulator, not a control surface for the loop.
    # Tests can opt out via OPENMARQUEE_DISABLE_AUTOSTART=1 so fixtures
    # that spin a backend don't have an extra asyncio task running.
    if os.environ.get("OPENMARQUEE_DISABLE_AUTOSTART") != "1":
        try:
            await get_playback_loop().start()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "startup playback autostart failed"
            )

    # Flock pull worker — periodic reliability backstop that reconciles
    # against sync=True peers even when pushes get dropped. Tests can
    # opt out via OPENMARQUEE_DISABLE_PULL_WORKER=1 so fixtures that
    # spin a backend don't have an extra asyncio task racing the assertions.
    if os.environ.get("OPENMARQUEE_DISABLE_PULL_WORKER") != "1":
        try:
            await get_pull_worker().start()
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "startup pull worker autostart failed"
            )
    yield
    await get_playback_loop().stop()
    try:
        await get_pull_worker().stop()
    except Exception:
        pass


app = FastAPI(title="openMarquee", version=__version__, lifespan=lifespan)


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
    sanitised = [
        {k: v for k, v in err.items() if k != "input"} for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(sanitised)},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Used by the dev script and (later) by systemd."""
    return {"status": "alive", "version": __version__}


app.include_router(content_router)
app.include_router(playlist_router)
app.include_router(schedule_router)
app.include_router(settings_router)
app.include_router(backgrounds_router)
app.include_router(playback_router)
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
