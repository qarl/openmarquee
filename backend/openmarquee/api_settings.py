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
re-render runs SYNCHRONOUSLY before PUT returns 200 — operators expect
a rotation flip to "do something" and the UI re-mounts every panel
immediately on the openmarquee:settings-updated event, so an async
BackgroundTask raced the re-mount and the new slide-browser fetched
GET /api/content before the rerender bumped updated_at on disk
(QA-flagged race 2026-04-30). Synchronous trades a ~1s rotation save
for a guaranteed-coherent post-save state.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

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
) -> SystemSettings:
    # Compare the dim-affecting fields BEFORE the save so we know whether
    # to fire the text-slide rerender. None of these are remotely mutable
    # independently — the UI does GET → mutate → PUT so the whole payload
    # is what arrives.
    previous = storage.load()
    dims_changed = (
        int(previous.display_rotation) != int(payload.display_rotation)
        or int(previous.display_width) != int(payload.display_width)
        or int(previous.display_height) != int(payload.display_height)
    )
    storage.save(payload)
    if dims_changed:
        # Synchronous: operator's UI re-mounts panels on the
        # settings-updated event; if rerender ran in the background
        # the re-mount would race ahead and fetch stale updated_at
        # from /api/content. Cost is ~1s on a rotation flip — rare,
        # and the operator already expects "something happens" on
        # this knob.
        rerender_text_slides_for_dims(
            content_storage,
            int(payload.display_rotation),
            int(payload.display_width),
            int(payload.display_height),
        )
    return payload
