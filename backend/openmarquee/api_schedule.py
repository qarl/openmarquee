"""REST API for schedule rules.

GET /api/schedules — current schedule
PUT /api/schedules — replace the schedule

Note: the endpoints persist the schedule but the playback engine doesn't
yet act on it (Phase 5 (d) ships the substrate; the multi-playlist
refactor that uses it lands separately).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from openmarquee.dependencies import get_schedule_storage
from openmarquee.schedule import Schedule, ScheduleStorage

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

ScheduleDep = Annotated[ScheduleStorage, Depends(get_schedule_storage)]


@router.get("", response_model=Schedule)
async def get_schedule(storage: ScheduleDep) -> Schedule:
    return storage.load()


@router.put("", response_model=Schedule)
async def set_schedule(payload: Schedule, storage: ScheduleDep) -> Schedule:
    # The model itself enforces all field constraints; FastAPI maps any
    # ValidationError on the wire to 422 automatically. Re-raise here only
    # to be explicit if we ever want a custom detail shape.
    try:
        storage.save(payload)
    except ValidationError:
        raise
    return payload
