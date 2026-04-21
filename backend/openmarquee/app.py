"""FastAPI application that runs on the device."""

from fastapi import FastAPI

from openmarquee import __version__
from openmarquee.api import router as content_router

app = FastAPI(title="OpenMarquee", version=__version__)
app.include_router(content_router)


@app.get("/")
async def index() -> dict[str, str]:
    return {"status": "alive", "version": __version__}
