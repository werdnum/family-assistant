"""Web UI routes for managing event listeners."""

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED

router = APIRouter(prefix="/event-listeners", tags=["listeners_ui"])


def format_match_conditions(conditions: dict) -> list[str]:
    """Format match conditions for display."""
    formatted = []
    for key, value in conditions.items():
        # Handle nested paths like "new_state.state"
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                formatted.append(f"{key}.{sub_key} = {sub_value}")
        else:
            formatted.append(f"{key} = {value}")
    return formatted


@router.get("", response_class=HTMLResponse)
async def listeners_list(
    request: Request,
    source_id: Annotated[str | None, Query(description="Filter by source")] = None,
    action_type: Annotated[
        str | None, Query(description="Filter by action type")
    ] = None,
    conversation_id: Annotated[
        str | None, Query(description="Filter by conversation ID")
    ] = None,
    enabled: Annotated[
        bool | None, Query(description="Filter by enabled status")
    ] = None,
    limit: Annotated[int, Query(description="Items per page")] = 50,
    offset: Annotated[int, Query(description="Page offset")] = 0,
) -> Any:
    """Display list of event listeners (administrative view)."""
    user = request.session.get("user")
    async with DatabaseContext() as db:
        # Always show all listeners (this is an admin interface)
        # But allow filtering by conversation_id if specified
        listeners, total_count = await db.events.get_all_event_listeners(
            source_id=source_id,
            action_type=action_type,
            conversation_id=conversation_id,
            enabled=enabled,
            limit=limit,
            offset=offset,
        )

    # Format timestamps and add icons
    for listener in listeners:
        if (
            listener.get("created_at")
            and hasattr(listener["created_at"], "tzinfo")
            and listener["created_at"].tzinfo is None
        ):
            listener["created_at"] = listener["created_at"].replace(tzinfo=timezone.utc)
        if (
            listener.get("last_execution_at")
            and hasattr(listener["last_execution_at"], "tzinfo")
            and listener["last_execution_at"].tzinfo is None
        ):
            listener["last_execution_at"] = listener["last_execution_at"].replace(
                tzinfo=timezone.utc
            )

        # Add action type icon
        listener["action_icon"] = (
            "🤖" if listener["action_type"] == "wake_llm" else "📜"
        )

    # Calculate pagination
    has_next = offset + limit < total_count
    has_prev = offset > 0
    next_offset = offset + limit if has_next else offset
    prev_offset = max(0, offset - limit)
    current_page = (offset // limit) + 1
    total_pages = max(1, (total_count + limit - 1) // limit)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "listeners/listeners_list.html.j2",
        {
            "request": request,
            "user": user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "listeners": listeners,
            "total_count": total_count,
            "source_id": source_id,
            "action_type": action_type,
            "conversation_id": conversation_id,
            "enabled": enabled,
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


@router.get("/{listener_id}", response_class=HTMLResponse)
async def listener_detail(
    request: Request,
    listener_id: int,
) -> Any:
    """Display listener details (administrative view)."""
    user = request.session.get("user")
    async with DatabaseContext() as db:
        # Always show the listener (this is an admin interface)
        listener = await db.events.get_event_listener_by_id(listener_id)

        if not listener:
            raise HTTPException(status_code=404, detail="Event listener not found")

        # Get execution statistics
        stats = await db.events.get_listener_execution_stats(listener_id)

        # For script listeners, get detailed task history
        task_executions = []
        task_total = 0
        if listener["action_type"] == "script":
            task_executions, task_total = await db.tasks.get_tasks_for_listener(
                listener_id, limit=20
            )

        # Format match conditions
        listener["formatted_conditions"] = format_match_conditions(
            listener.get("match_conditions", {})
        )

        # Add action icon
        listener["action_icon"] = (
            "🤖" if listener["action_type"] == "wake_llm" else "📜"
        )

        # Format timestamps
        if (
            listener.get("created_at")
            and hasattr(listener["created_at"], "tzinfo")
            and listener["created_at"].tzinfo is None
        ):
            listener["created_at"] = listener["created_at"].replace(tzinfo=timezone.utc)
        if (
            listener.get("last_execution_at")
            and hasattr(listener["last_execution_at"], "tzinfo")
            and listener["last_execution_at"].tzinfo is None
        ):
            listener["last_execution_at"] = listener["last_execution_at"].replace(
                tzinfo=timezone.utc
            )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "listeners/listener_detail.html.j2",
        {
            "request": request,
            "user": user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "listener": listener,
            "stats": stats,
            "task_executions": task_executions,
            "task_total": task_total,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@router.post("/{listener_id}/toggle", response_class=HTMLResponse)
async def toggle_listener(
    request: Request,
    listener_id: int,
    enabled: Annotated[bool, Form()],
) -> Any:
    """Toggle listener enabled status."""
    user = request.session.get("user")
    async with DatabaseContext() as db:
        # Check permissions unless admin
        if user and user.get("admin_mode"):
            listener = await db.events.get_event_listener_by_id(listener_id)
            if not listener:
                raise HTTPException(status_code=404, detail="Event listener not found")
            conversation_id = listener["conversation_id"]
        else:
            conversation_id = user.get("conversation_id", "") if user else ""

        success = await db.events.update_event_listener_enabled(
            listener_id, conversation_id, enabled
        )

        if not success:
            raise HTTPException(status_code=404, detail="Event listener not found")

    # Redirect back to detail page
    return RedirectResponse(
        url=f"/event-listeners/{listener_id}",
        status_code=303,  # See Other
    )


@router.post("/{listener_id}/delete", response_class=HTMLResponse)
async def delete_listener(
    request: Request,
    listener_id: int,
) -> Any:
    """Delete a listener."""
    user = request.session.get("user")
    async with DatabaseContext() as db:
        # Check permissions unless admin
        if user and user.get("admin_mode"):
            listener = await db.events.get_event_listener_by_id(listener_id)
            if not listener:
                raise HTTPException(status_code=404, detail="Event listener not found")
            conversation_id = listener["conversation_id"]
        else:
            conversation_id = user.get("conversation_id", "") if user else ""

        success = await db.events.delete_event_listener(listener_id, conversation_id)

        if not success:
            raise HTTPException(status_code=404, detail="Event listener not found")

    # Redirect to list page
    return RedirectResponse(
        url="/event-listeners",
        status_code=303,  # See Other
    )
