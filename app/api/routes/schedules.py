from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import PaginationDep, SchedulingServiceDep, SessionDep
from app.api.responses import ERRORS
from app.core.exceptions import NotFoundError
from app.models.scheduled_post import ScheduledPost
from app.repositories.schedule_repository import ScheduleRepository
from app.schemas.common import Page
from app.schemas.schedule import ScheduleCreate, ScheduleRead

router = APIRouter(prefix="/schedules", tags=["Schedules"], responses=ERRORS)


@router.post("", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED, summary="Schedule a post",
             description="`scheduled_at` must be timezone-aware and in the future; it is stored in UTC. At that "
                         "time the scheduler re-validates the post, accounts, tokens and capabilities, then "
                         "dispatches the publication jobs.")
async def create_schedule(body: ScheduleCreate, service: SchedulingServiceDep) -> ScheduledPost:
    return await service.create(body.post_id, body.scheduled_at, body.platforms)


@router.get("", response_model=Page[ScheduleRead], summary="List schedules")
async def list_schedules(session: SessionDep, page: PaginationDep, post_id: uuid.UUID | None = None,
                         status_filter: Annotated[str | None, Query(alias="status")] = None) -> dict:
    items, total = await ScheduleRepository(session).list(
        status=status_filter.upper() if status_filter else None, post_id=post_id, limit=page.limit,
        offset=page.offset)
    return {"items": items, "total": total, "limit": page.limit, "offset": page.offset}


@router.get("/{schedule_id}", response_model=ScheduleRead, summary="Get a schedule")
async def get_schedule(schedule_id: uuid.UUID, session: SessionDep) -> ScheduledPost:
    schedule = await ScheduleRepository(session).get(schedule_id)
    if schedule is None:
        raise NotFoundError("Schedule not found.", code="SCHEDULE_NOT_FOUND")
    return schedule


@router.delete("/{schedule_id}", response_model=ScheduleRead, summary="Cancel a pending schedule")
async def cancel_schedule(schedule_id: uuid.UUID, service: SchedulingServiceDep) -> ScheduledPost:
    return await service.cancel(schedule_id)
