"""REST API for device system settings.

GET /api/settings — current settings (defaults if nothing persisted yet)
PUT /api/settings — replace settings

Writes validate the whole model, so partial updates are an explicit GET,
mutate, PUT client round-trip. The UI does exactly that on Save; keeping
the server PUT-only means we never have to reason about which field is
authoritative in a race.

PUT also fires a side-effect when display dims change (rotation OR
width OR height): every saved text slide gets re-rendered at the new
effective dims so its asset.png matches what the device will display
instead of being letterboxed / cover-fitted from stale dims. The
re-render runs as a BackgroundTask so the operator's PUT response
isn't gated by potentially many slide renders.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_content_storage, get_settings_storage
from openmarquee.settings import SettingsStorage, SystemSettings
from openmarquee.text_rerender import rerender_text_slides_for_dims

router = APIRouter(prefix="/api/settings", tags=["settings"])

SettingsDep = Annotated[SettingsStorage, Depends(get_settings_storage)]
ContentDep = Annotated[ContentStorage, Depends(get_content_storage)]


@router.get("", response_model=SystemSettings)
async def get_settings(storage: SettingsDep) -> SystemSettings:
    return storage.load()


@router.put("", response_model=SystemSettings)
async def set_settings(
    payload: SystemSettings,
    storage: SettingsDep,
    content_storage: ContentDep,
    background: BackgroundTasks,
) -> SystemSettings:
    # Compare the dim-affecting fields BEFORE the save so we can decide
    # whether to schedule a text-slide rerender. None of these are
    # remotely mutable independently — the UI does GET → mutate → PUT
    # so the whole payload is what arrives.
    previous = storage.load()
    dims_changed = (
        int(previous.display_rotation) != int(payload.display_rotation)
        or int(previous.display_width) != int(payload.display_width)
        or int(previous.display_height) != int(payload.display_height)
    )
    storage.save(payload)
    if dims_changed:
        background.add_task(
            rerender_text_slides_for_dims,
            content_storage,
            int(payload.display_rotation),
            int(payload.display_width),
            int(payload.display_height),
        )
    return payload
