"""Web UI routes for managing event listeners."""

import json
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED, get_user_from_request

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
    user = get_user_from_request(request)
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
    user = get_user_from_request(request)
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
    user = get_user_from_request(request)
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
    user = get_user_from_request(request)
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


@router.post("/api/validate-script")
async def validate_script(
    request: Request,
) -> Any:
    """Validate a Starlark script and return diagnostics."""
    from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine

    try:
        body = await request.json()
        script_code = body.get("script_code", "")
        sample_event = body.get("sample_event")

        # Create a validation engine with minimal timeout
        engine = StarlarkEngine(
            tools_provider=None,  # No tools for validation
            config=StarlarkConfig(
                max_execution_time=1,  # 1 second max for validation
                deny_all_tools=True,
            ),
        )

        if sample_event:
            # Test execution with sample event
            context = {
                "event": sample_event,
                "conversation_id": "test_validation",
                "listener_id": "test_listener",
            }
            await engine.evaluate_async(
                script=script_code,
                globals_dict=context,
                execution_context=None,
            )
        else:
            # Just parse the script for syntax errors
            # The engine will parse during evaluate_async, so we'll do a minimal test
            await engine.evaluate_async(
                script=script_code,
                globals_dict={"event": {}},
                execution_context=None,
            )

        return {"valid": True, "diagnostics": []}

    except Exception as e:
        # Extract error details
        error_msg = str(e)
        line = 1
        column = 1

        # Try to extract line number from common error formats
        if "line " in error_msg:
            import re

            match = re.search(r"line (\d+)", error_msg)
            if match:
                line = int(match.group(1))

        return {
            "valid": False,
            "diagnostics": [
                {
                    "line": line,
                    "column": column,
                    "message": error_msg,
                    "severity": "error",
                }
            ],
        }


@router.get("/new", response_class=HTMLResponse)
async def new_listener(
    request: Request,
) -> Any:
    """Display form for creating a new event listener."""
    user = get_user_from_request(request)

    # Create a default listener object for the template
    default_listener = {
        "id": None,
        "name": "",
        "source_id": "home_assistant",
        "action_type": "wake_llm",
        "match_conditions": {},
        "action_config": {"script_code": "", "timeout": 600, "llm_callback_prompt": ""},
        "description": "",
        "enabled": True,
        "one_time": False,
    }

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "listeners/listener_edit.html.j2",
        {
            "request": request,
            "user": user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "listener": default_listener,
            "is_new": True,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@router.get("/{listener_id}/edit", response_class=HTMLResponse)
async def edit_listener(
    request: Request,
    listener_id: int,
) -> Any:
    """Display edit form for event listener."""
    user = get_user_from_request(request)
    async with DatabaseContext() as db:
        # Check permissions unless admin
        if user and user.get("admin_mode"):
            listener = await db.events.get_event_listener_by_id(listener_id)
        else:
            conversation_id = user.get("conversation_id", "") if user else ""
            listener = await db.events.get_event_listener_by_id(
                listener_id, conversation_id
            )

        if not listener:
            raise HTTPException(status_code=404, detail="Event listener not found")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "listeners/listener_edit.html.j2",
        {
            "request": request,
            "user": user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "listener": listener,
            "is_new": False,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@router.post("/{listener_id}/update", response_class=HTMLResponse)
async def update_listener(
    request: Request,
    listener_id: int,
    name: Annotated[str, Form()],
    match_conditions: Annotated[str, Form()],  # JSON string
    description: Annotated[str | None, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    one_time: Annotated[str | None, Form()] = None,
    # Script-specific fields
    script_code: Annotated[str | None, Form()] = None,
    timeout: Annotated[int | None, Form()] = None,
    # LLM-specific fields
    llm_callback_prompt: Annotated[str | None, Form()] = None,
) -> Any:
    """Handle listener update."""
    user = get_user_from_request(request)

    try:
        # Parse match conditions JSON
        try:
            match_conditions_dict = json.loads(match_conditions)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400, detail="Invalid match conditions JSON"
            ) from e

        # Convert checkbox values to booleans
        enabled_bool = enabled is not None
        one_time_bool = one_time is not None

        async with DatabaseContext() as db:
            # Get the existing listener to check action type and permissions
            if user and user.get("admin_mode"):
                existing = await db.events.get_event_listener_by_id(listener_id)
                if not existing:
                    raise HTTPException(
                        status_code=404, detail="Event listener not found"
                    )
                conversation_id = existing["conversation_id"]
            else:
                conversation_id = user.get("conversation_id", "") if user else ""
                existing = await db.events.get_event_listener_by_id(
                    listener_id, conversation_id
                )
                if not existing:
                    raise HTTPException(
                        status_code=404, detail="Event listener not found"
                    )

            # Build action_config based on action type
            action_config = None
            if existing["action_type"] == "script":
                if script_code is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Script code is required for script listeners",
                    )
                action_config = {
                    "script_code": script_code,
                    "timeout": timeout or 600,
                }
            elif existing["action_type"] == "wake_llm":
                action_config = {}
                if llm_callback_prompt:
                    action_config["llm_callback_prompt"] = llm_callback_prompt

            # Update the listener
            success = await db.events.update_event_listener(
                listener_id=listener_id,
                conversation_id=conversation_id,
                name=name,
                description=description or None,
                match_conditions=match_conditions_dict,
                action_config=action_config,
                one_time=one_time_bool,
                enabled=enabled_bool,
            )

            if not success:
                raise HTTPException(
                    status_code=500, detail="Failed to update event listener"
                )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error updating listener: {str(e)}"
        ) from e

    # Redirect back to detail page
    return RedirectResponse(
        url=f"/event-listeners/{listener_id}",
        status_code=303,  # See Other
    )


@router.post("/create", response_class=HTMLResponse)
async def create_listener(
    request: Request,
    name: Annotated[str, Form()],
    source_id: Annotated[str, Form()],
    action_type: Annotated[str, Form()],
    match_conditions: Annotated[str, Form()],  # JSON string
    description: Annotated[str | None, Form()] = "",
    enabled: Annotated[str | None, Form()] = None,
    one_time: Annotated[str | None, Form()] = None,
    # Script-specific fields
    script_code: Annotated[str | None, Form()] = None,
    timeout: Annotated[int | None, Form()] = None,
    # LLM-specific fields
    llm_callback_prompt: Annotated[str | None, Form()] = None,
) -> Any:
    """Handle new listener creation."""
    user = get_user_from_request(request)
    conversation_id = user.get("conversation_id", "") if user else ""

    try:
        # Parse match conditions JSON
        try:
            match_conditions_dict = json.loads(match_conditions)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400, detail="Invalid match conditions JSON"
            ) from e

        # Convert checkbox values to booleans
        enabled_bool = enabled is not None
        one_time_bool = one_time is not None

        # Build action_config based on action type
        action_config = None
        if action_type == "script":
            if script_code is None:
                raise HTTPException(
                    status_code=400,
                    detail="Script code is required for script listeners",
                )
            action_config = {
                "script_code": script_code,
                "timeout": timeout or 600,
            }
        elif action_type == "wake_llm":
            action_config = {}
            if llm_callback_prompt:
                action_config["llm_callback_prompt"] = llm_callback_prompt

        async with DatabaseContext() as db:
            # Create the listener
            listener_id = await db.events.create_event_listener(
                name=name,
                source_id=source_id,
                match_conditions=match_conditions_dict,
                conversation_id=conversation_id,
                interface_type="web",
                action_type=action_type,
                action_config=action_config,
                description=description or None,
                one_time=one_time_bool,
                enabled=enabled_bool,
            )

    except HTTPException:
        raise
    except Exception as e:
        # Check for unique constraint violation
        error_str = str(e)
        if "UNIQUE constraint failed" in error_str and "name" in error_str:
            raise HTTPException(
                status_code=400,
                detail=f"An event listener named '{name}' already exists in this conversation",
            ) from e
        raise HTTPException(
            status_code=500, detail=f"Error creating listener: {str(e)}"
        ) from e

    # Redirect to the new listener's detail page
    return RedirectResponse(
        url=f"/event-listeners/{listener_id}",
        status_code=303,  # See Other
    )
