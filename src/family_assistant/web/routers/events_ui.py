"""Web UI routes for viewing events."""

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED

router = APIRouter(prefix="/events", tags=["events_ui"])


def format_event_summary(event: dict) -> str:
    """Create a summary of event data for display."""
    event_data = event.get("event_data", {})

    # Try to extract meaningful info based on source
    if event.get("source_id") == "home_assistant":
        entity_id = event_data.get("entity_id", "Unknown entity")
        new_state = event_data.get("new_state", {}).get("state", "Unknown")
        return f"{entity_id}: {new_state}"
    elif event.get("source_id") == "indexing":
        doc_type = event_data.get("document_type", "Unknown type")
        title = event_data.get("metadata", {}).get("title", "Unknown document")
        return f"{doc_type}: {title}"
    else:
        # Generic summary - first 100 chars of str(event_data)
        summary = str(event_data)
        return summary[:100] + "..." if len(summary) > 100 else summary


@router.get("", response_class=HTMLResponse)
async def events_list(
    request: Request,
    source_id: Annotated[str | None, Query(description="Filter by source")] = None,
    hours: Annotated[int, Query(description="Hours to look back")] = 24,
    only_triggered: Annotated[
        bool, Query(description="Only show events that triggered listeners")
    ] = False,
    limit: Annotated[int, Query(description="Items per page")] = 50,
    offset: Annotated[int, Query(description="Page offset")] = 0,
) -> Any:
    """Display list of recent events."""
    async with DatabaseContext() as db:
        events, total_count = await db.events.get_events_with_listeners(
            source_id=source_id,
            hours=hours,
            limit=limit,
            offset=offset,
            only_triggered=only_triggered,
        )

    # Process events for display
    for event in events:
        event["summary"] = format_event_summary(event)
        # Format timestamp
        if (
            event.get("timestamp")
            and hasattr(event["timestamp"], "tzinfo")
            and event["timestamp"].tzinfo is None
        ):
            event["timestamp"] = event["timestamp"].replace(tzinfo=timezone.utc)

    # Calculate pagination
    has_next = offset + limit < total_count
    has_prev = offset > 0
    next_offset = offset + limit if has_next else offset
    prev_offset = max(0, offset - limit)
    current_page = (offset // limit) + 1
    total_pages = max(1, (total_count + limit - 1) // limit)

    templates = request.app.state.templates
    user = request.session.get("user")
    return templates.TemplateResponse(
        "events/events_list.html.j2",
        {
            "request": request,
            "user": user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "events": events,
            "total_count": total_count,
            "source_id": source_id,
            "hours": hours,
            "only_triggered": only_triggered,
            "limit": limit,
            "offset": offset,
            "has_next": has_next,
            "has_prev": has_prev,
            "next_offset": next_offset,
            "prev_offset": prev_offset,
            "current_page": current_page,
            "total_pages": total_pages,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@router.get("/{event_id}", response_class=HTMLResponse)
async def event_detail(
    request: Request,
    event_id: str,
) -> Any:
    """Display event details."""
    async with DatabaseContext() as db:
        event = await db.events.get_event_by_id(event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        # Get triggered listeners details
        triggered_listeners = []
        if event.get("triggered_listener_ids"):
            for listener_id in event["triggered_listener_ids"]:
                listener = await db.events.get_event_listener_by_id(listener_id)
                if listener:
                    # Check task execution status for script listeners
                    if listener["action_type"] == "script":
                        # Get the most recent execution task
                        tasks, _ = await db.tasks.get_tasks_for_listener(
                            listener_id, limit=1
                        )
                        if tasks:
                            listener["last_execution"] = tasks[0]
                    triggered_listeners.append(listener)

        # Get all active listeners for this source to show why they didn't trigger
        user = request.session.get("user")
        potential_listeners = []
        if user and user.get("admin_mode"):
            # Admin sees all listeners
            all_listeners, _ = await db.events.get_all_event_listeners(
                source_id=event.get("source_id"),
                enabled=True,
            )
        else:
            # Regular user sees only their listeners
            all_listeners = await db.events.get_event_listeners(
                conversation_id=user["conversation_id"] if user else "",
                source_id=event.get("source_id"),
                enabled=True,
            )

        # Filter out triggered listeners to get potential ones
        triggered_ids = set(event.get("triggered_listener_ids", []))
        for listener in all_listeners:
            if listener["id"] not in triggered_ids:
                potential_listeners.append(listener)

    # Format timestamps
    if (
        event.get("timestamp")
        and hasattr(event["timestamp"], "tzinfo")
        and event["timestamp"].tzinfo is None
    ):
        event["timestamp"] = event["timestamp"].replace(tzinfo=timezone.utc)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "events/event_detail.html.j2",
        {
            "request": request,
            "user": user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "event": event,
            "triggered_listeners": triggered_listeners,
            "potential_listeners": potential_listeners,
            "now_utc": datetime.now(timezone.utc),
        },
    )
