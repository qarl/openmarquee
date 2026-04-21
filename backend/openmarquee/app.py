"""FastAPI application that runs on the device."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from openmarquee import __version__
from openmarquee.api import router as content_router
from openmarquee.dev import router as dev_router

app = FastAPI(title="OpenMarquee", version=__version__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Used by the dev script and (later) by systemd."""
    return {"status": "alive", "version": __version__}


app.include_router(content_router)

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
