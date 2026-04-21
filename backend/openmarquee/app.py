"""FastAPI application that runs on the device."""

from fastapi import FastAPI

from openmarquee import __version__

app = FastAPI(title="OpenMarquee", version=__version__)


@app.get("/")
async def index() -> dict[str, str]:
    return {"status": "alive", "version": __version__}
