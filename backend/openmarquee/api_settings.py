"""REST API for device system settings.

GET /api/settings — current settings (defaults if nothing persisted yet)
PUT /api/settings — replace settings

Writes validate the whole model, so partial updates are an explicit GET,
mutate, PUT client round-trip. The UI does exactly that on Save; keeping
the server PUT-only means we never have to reason about which field is
authoritative in a race.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from openmarquee.dependencies import get_settings_storage
from openmarquee.settings import SettingsStorage, SystemSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])

SettingsDep = Annotated[SettingsStorage, Depends(get_settings_storage)]


@router.get("", response_model=SystemSettings)
async def get_settings(storage: SettingsDep) -> SystemSettings:
    return storage.load()


@router.put("", response_model=SystemSettings)
async def set_settings(payload: SystemSettings, storage: SettingsDep) -> SystemSettings:
    storage.save(payload)
    return payload
