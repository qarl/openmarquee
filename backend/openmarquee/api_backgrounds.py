"""REST API for AI-generated backgrounds.

POST /api/backgrounds/generate — prompt in, ImageSlide out.

Captive-portal auth (i.e. no auth — anyone on the WiFi can hit this).
The shipped provider set is free-tier services; nothing here depends on
a paid API key. See `openmarquee.backgrounds` for the provider registry.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openmarquee.backgrounds import (
    PROVIDERS,
    BackgroundGenError,
    BackgroundProviderUnknown,
    default_provider_name,
    resolve_provider,
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
    # 500 chars matches the typical SD / generic-image-gen prompt budget;
    # longer prompts hit upstream provider limits before they help. The
    # earlier 4000-char cap was effectively no cap (a 2 KB prompt sailed
    # through unnoticed in QA's exploratory probe — explore-bg-gen.md).
    # Defensive: catches operator typos / pasted novels at the API edge
    # rather than wasting a Pollinations round-trip to find out.
    prompt: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(
        default=None,
        description="One of the shipped provider names, e.g. 'pollinations'. "
        "Omit to use the device's default.",
    )


class ProvidersResponse(BaseModel):
    default: str
    available: list[str]


def _append(playlist_storage: PlaylistStorage, item_id) -> None:
    playlist = playlist_storage.load()
    playlist.append(item_id)
    playlist_storage.save(playlist)


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """Which image-gen services this device knows about + the default one."""
    return ProvidersResponse(
        default=default_provider_name(),
        available=sorted(PROVIDERS),
    )


@router.post("/generate", response_model=ImageSlide)
async def generate_background(
    payload: BackgroundGenerateRequest,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    settings_storage: SettingsDep,
) -> ImageSlide:
    try:
        provider = resolve_provider(payload.provider)
    except BackgroundProviderUnknown as exc:
        # 400 = the caller's request was malformed (named provider that
        # isn't in our registry).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        raw = provider.generate(payload.prompt)
    except BackgroundGenError as exc:
        # 502 = upstream-bad-gateway. The detail surfaces the provider's
        # own message (rate-limit, timeout, etc.) so the operator can act.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Store the provider's bytes verbatim — no device-side resample.
    # Playback cover-fits down to panel dims on slide entry, so keeping
    # the original 1024×1024 (or whatever the provider returned) means
    # a panel-resolution change doesn't degrade the generated slide.
    slide = ImageSlide(
        name=payload.name or _name_from_prompt(payload.prompt),
        duration_ms=5000,
    )
    storage.save_image(slide, raw)
    _append(playlist_storage, slide.id)
    return slide


def _name_from_prompt(prompt: str) -> str:
    """Turn a prompt into a reasonable slide name. Truncate hard because
    the name column in the saved-slides list is narrow; operators rename
    anyway."""
    # 9.3: empty/whitespace-only prompt -> splitlines() returns []
    # and [0] would IndexError. Guard with `or [""]` so the truthy
    # tail falls through to the generic placeholder.
    first_line = (prompt.strip().splitlines() or [""])[0]
    trimmed = first_line[:60].strip()
    return f"{trimmed} — Background" if trimmed else "Generated — Background"
