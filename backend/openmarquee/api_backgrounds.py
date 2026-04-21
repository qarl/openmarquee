"""REST API for AI-generated backgrounds.

POST /api/backgrounds/generate — prompt in, ImageSlide out.

Auth is unchanged (we're on the captive portal; anyone on the WiFi can
hit this). Cost is the operator's concern — they pay for the OPENAI_API_KEY
that this reads from the environment.
"""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openmarquee.backgrounds import (
    OpenAIError,
    OpenAINotConfigured,
    downscale_to_panel,
    generate_png_via_openai,
)
from openmarquee.content import ImageSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_content_storage,
    get_playlist_storage,
    get_settings_storage,
)
from openmarquee.playlist import PlaylistStorage
from openmarquee.settings import SettingsStorage

router = APIRouter(prefix="/api/backgrounds", tags=["backgrounds"])

StorageDep = Annotated[ContentStorage, Depends(get_content_storage)]
PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]
SettingsDep = Annotated[SettingsStorage, Depends(get_settings_storage)]


class BackgroundGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, max_length=200)


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise OpenAINotConfigured(
            "OPENAI_API_KEY is not set on the device. Provision it via the "
            "systemd unit env or a root-only .env and restart to enable "
            "AI-generated backgrounds."
        )
    return key


def _append(playlist_storage: PlaylistStorage, item_id) -> None:
    playlist = playlist_storage.load()
    playlist.append(item_id)
    playlist_storage.save(playlist)


@router.post("/generate", response_model=ImageSlide)
async def generate_background(
    payload: BackgroundGenerateRequest,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    settings_storage: SettingsDep,
) -> ImageSlide:
    try:
        key = _api_key()
    except OpenAINotConfigured as exc:
        # 503 "Service Unavailable" signals "this endpoint exists but
        # isn't turned on for this device" — the browser can render a
        # friendly message instead of crashing on 500.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        png_raw = generate_png_via_openai(payload.prompt, key)
    except OpenAIError as exc:
        # 502 = upstream-bad-gateway. The detail surfaces OpenAI's own
        # message (content policy, quota, auth) so the operator can
        # actually act on it.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    settings = settings_storage.load()
    png = downscale_to_panel(png_raw, settings.display_width, settings.display_height)

    slide = ImageSlide(
        name=payload.name or _name_from_prompt(payload.prompt),
        duration_ms=5000,
    )
    storage.save_image(slide, png)
    _append(playlist_storage, slide.id)
    return slide


def _name_from_prompt(prompt: str) -> str:
    """Turn a prompt into a reasonable slide name. Truncate hard because
    the name column in the saved-slides list is narrow; operators rename
    anyway."""
    trimmed = prompt.strip().splitlines()[0][:60].strip()
    return f"Background — {trimmed}" if trimmed else "Background — Generated"
