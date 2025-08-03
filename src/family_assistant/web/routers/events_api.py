from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

events_api_router = APIRouter()


class EventModel(BaseModel):
    event_id: str
    source_id: str | None = None
    event_data: dict | None = None
    triggered_listener_ids: list[int] | None = None
    timestamp: datetime | None = None


class EventsListResponse(BaseModel):
    events: list[EventModel]
    total: int


@events_api_router.get("/")
async def list_events(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    source_id: str | None = None,
    hours: Annotated[int, Query(ge=1)] = 24,
    only_triggered: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> EventsListResponse:
    """Return recent events."""
    events, total = await db_context.events.get_events_with_listeners(
        source_id=source_id,
        hours=hours,
        limit=limit,
        offset=offset,
        only_triggered=only_triggered,
    )
    return EventsListResponse(
        events=[EventModel(**event) for event in events], total=total
    )


@events_api_router.get("/{event_id}")
async def get_event(
    event_id: str, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> EventModel:
    """Return details for a single event."""
    event = await db_context.events.get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return EventModel(**event)
