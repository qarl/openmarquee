"""FastAPI application that runs on the device."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from openmarquee import __version__
from openmarquee.api import router as content_router
from openmarquee.api_playback import router as playback_router
from openmarquee.api_playlist import router as playlist_router
from openmarquee.api_schedule import router as schedule_router
from openmarquee.api_settings import router as settings_router
from openmarquee.dependencies import (
    get_content_storage,
    get_demo_video_path,
    get_playback_loop,
    get_playlist_storage,
    get_seed_marker_path,
    get_settings_storage,
)
from openmarquee.dev import router as dev_router
from openmarquee.seed import seed_if_needed


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: first-boot seed. Shutdown: stop the playback loop."""
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
    yield
    await get_playback_loop().stop()


app = FastAPI(title="OpenMarquee", version=__version__, lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Used by the dev script and (later) by systemd."""
    return {"status": "alive", "version": __version__}


app.include_router(content_router)
app.include_router(playlist_router)
app.include_router(schedule_router)
app.include_router(settings_router)
app.include_router(playback_router)

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
