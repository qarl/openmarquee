"""FastAPI application that runs on the device."""

import os

from fastapi import FastAPI

from openmarquee import __version__
from openmarquee.api import router as content_router
from openmarquee.dev import router as dev_router

app = FastAPI(title="OpenMarquee", version=__version__)
app.include_router(content_router)

# Dev tooling (preview page, manual play endpoint) is mounted by default
# because the device is its own captive-portal AP with no inbound internet.
# The SD-card image builder (Phase 9) sets OPENMARQUEE_DISABLE_DEV=1 to
# strip it from production images.
if os.environ.get("OPENMARQUEE_DISABLE_DEV") != "1":
    app.include_router(dev_router)


@app.get("/")
async def index() -> dict[str, str]:
    return {"status": "alive", "version": __version__}
