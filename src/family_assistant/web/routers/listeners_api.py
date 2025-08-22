"""API endpoints for event listeners management."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)

listeners_api_router = APIRouter(tags=["Event Listeners"])


class EventListenerResponse(BaseModel):
    """Response model for event listener data."""

    id: int
    name: str
    source_id: str
    action_type: str
    match_conditions: dict[str, Any]
    action_config: dict[str, Any]
    description: str | None
    enabled: bool
    one_time: bool
    created_at: str
    last_execution_at: str | None
    daily_executions: int
    conversation_id: str
    interface_type: str
    condition_script: str | None


class EventListenersListResponse(BaseModel):
    """Response model for list of event listeners."""

    listeners: list[EventListenerResponse]
    total_count: int
    page: int
    page_size: int


class CreateEventListenerRequest(BaseModel):
    """Request model for creating an event listener."""

    name: str = Field(..., description="Unique name for the listener")
    source_id: str = Field(
        ..., description="Event source: home_assistant, indexing, or webhook"
    )
    action_type: str = Field(..., description="Action type: wake_llm or script")
    match_conditions: dict[str, Any] = Field(
        ..., description="Conditions to match events"
    )
    action_config: dict[str, Any] = Field(
        default_factory=dict, description="Configuration for the action"
    )
    description: str | None = Field(None, description="Optional description")
    enabled: bool = Field(True, description="Whether the listener is enabled")
    one_time: bool = Field(False, description="Auto-disable after first trigger")
    conversation_id: str = Field(..., description="Conversation ID for the listener")
    condition_script: str | None = Field(
        None,
        description="Optional Starlark script for condition matching (executed in sandboxed environment)",
    )


class UpdateEventListenerRequest(BaseModel):
    """Request model for updating an event listener."""

    name: str | None = None
    match_conditions: dict[str, Any] | None = None
    action_config: dict[str, Any] | None = None
    description: str | None = None
    enabled: bool | None = None
    one_time: bool | None = None
    condition_script: str | None = None


@listeners_api_router.get("")
async def list_event_listeners(
    db: Annotated[DatabaseContext, Depends(get_db)],
    source_id: Annotated[
        str | None, Query(description="Filter by event source")
    ] = None,
    action_type: Annotated[
        str | None, Query(description="Filter by action type")
    ] = None,
    conversation_id: Annotated[
        str | None, Query(description="Filter by conversation ID")
    ] = None,
    enabled: Annotated[
        bool | None, Query(description="Filter by enabled status")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EventListenersListResponse:
    """List event listeners with optional filters."""
    offset = (page - 1) * page_size

    listeners, total_count = await db.events.get_all_event_listeners(
        source_id=source_id,
        action_type=action_type,
        conversation_id=conversation_id,
        enabled=enabled,
        limit=page_size,
        offset=offset,
    )

    # Format listeners for response
    formatted_listeners = []
    for listener in listeners:
        formatted_listeners.append(
            EventListenerResponse(
                id=listener["id"],
                name=listener["name"],
                source_id=listener["source_id"],
                action_type=listener["action_type"],
                match_conditions=listener["match_conditions"],
                action_config=listener["action_config"],
                description=listener["description"],
                enabled=listener["enabled"],
                one_time=listener["one_time"],
                created_at=listener["created_at"].isoformat(),
                last_execution_at=(
                    listener["last_execution_at"].isoformat()
                    if listener["last_execution_at"]
                    else None
                ),
                daily_executions=listener["daily_executions"],
                conversation_id=listener["conversation_id"],
                interface_type=listener["interface_type"],
                condition_script=listener["condition_script"],
            )
        )

    return EventListenersListResponse(
        listeners=formatted_listeners,
        total_count=total_count,
        page=page,
        page_size=page_size,
    )


@listeners_api_router.get("/{listener_id}")
async def get_event_listener(
    listener_id: int,
    db: Annotated[DatabaseContext, Depends(get_db)],
    conversation_id: Annotated[
        str | None, Query(description="Conversation ID for permission check")
    ] = None,
) -> EventListenerResponse:
    """Get a specific event listener by ID."""
    listener = await db.events.get_event_listener_by_id(listener_id, conversation_id)

    if not listener:
        raise HTTPException(status_code=404, detail="Event listener not found")

    return EventListenerResponse(
        id=listener["id"],
        name=listener["name"],
        source_id=listener["source_id"],
        action_type=listener["action_type"],
        match_conditions=listener["match_conditions"],
        action_config=listener["action_config"],
        description=listener["description"],
        enabled=listener["enabled"],
        one_time=listener["one_time"],
        created_at=listener["created_at"].isoformat(),
        last_execution_at=(
            listener["last_execution_at"].isoformat()
            if listener["last_execution_at"]
            else None
        ),
        daily_executions=listener["daily_executions"],
        conversation_id=listener["conversation_id"],
        interface_type=listener["interface_type"],
        condition_script=listener["condition_script"],
    )


@listeners_api_router.post("")
async def create_event_listener(
    request: CreateEventListenerRequest,
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> EventListenerResponse:
    """Create a new event listener."""
    # Validate source_id
    valid_sources = ["home_assistant", "indexing", "webhook"]
    if request.source_id not in valid_sources:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source_id. Must be one of: {', '.join(valid_sources)}",
        )

    # Validate action_type
    valid_actions = ["wake_llm", "script"]
    if request.action_type not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action_type. Must be one of: {', '.join(valid_actions)}",
        )

    # Validate script requirements
    if request.action_type == "script" and not request.action_config.get("script_code"):
        raise HTTPException(
            status_code=400,
            detail="script_code is required in action_config when action_type is 'script'",
        )

    try:
        listener_id = await db.events.create_event_listener(
            name=request.name,
            source_id=request.source_id,
            match_conditions=request.match_conditions,
            conversation_id=request.conversation_id,
            interface_type="web",
            action_type=request.action_type,
            action_config=request.action_config,
            description=request.description,
            condition_script=request.condition_script,
            one_time=request.one_time,
            enabled=request.enabled,
        )

        # Fetch the created listener
        listener = await db.events.get_event_listener_by_id(listener_id)
        if not listener:
            raise HTTPException(
                status_code=500, detail="Failed to fetch created listener"
            )

    except Exception as e:
        error_str = str(e)
        if "UNIQUE constraint failed" in error_str and "name" in error_str:
            raise HTTPException(
                status_code=400,
                detail=f"An event listener named '{request.name}' already exists in this conversation",
            ) from e
        logger.error(f"Error creating event listener: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to create listener: {str(e)}"
        ) from e

    return EventListenerResponse(
        id=listener["id"],
        name=listener["name"],
        source_id=listener["source_id"],
        action_type=listener["action_type"],
        match_conditions=listener["match_conditions"],
        action_config=listener["action_config"],
        description=listener["description"],
        enabled=listener["enabled"],
        one_time=listener["one_time"],
        created_at=listener["created_at"].isoformat(),
        last_execution_at=None,
        daily_executions=0,
        conversation_id=listener["conversation_id"],
        interface_type=listener["interface_type"],
        condition_script=listener["condition_script"],
    )


@listeners_api_router.patch("/{listener_id}")
async def update_event_listener(
    listener_id: int,
    request: UpdateEventListenerRequest,
    conversation_id: Annotated[
        str, Query(description="Conversation ID for permission check")
    ],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> EventListenerResponse:
    """Update an existing event listener."""
    # Check if listener exists and user has permission
    existing = await db.events.get_event_listener_by_id(listener_id, conversation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Event listener not found")

    # Update the listener - merge with existing values for partial updates
    success = await db.events.update_event_listener(
        listener_id=listener_id,
        conversation_id=conversation_id,
        name=request.name if request.name is not None else existing["name"],
        description=request.description
        if request.description is not None
        else existing["description"],
        match_conditions=request.match_conditions
        if request.match_conditions is not None
        else existing["match_conditions"],
        action_config=request.action_config
        if request.action_config is not None
        else existing["action_config"],
        one_time=request.one_time
        if request.one_time is not None
        else existing["one_time"],
        enabled=request.enabled if request.enabled is not None else existing["enabled"],
        condition_script=request.condition_script
        if request.condition_script is not None
        else existing["condition_script"],
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to update event listener")

    # Fetch the updated listener
    listener = await db.events.get_event_listener_by_id(listener_id)
    if not listener:
        raise HTTPException(status_code=500, detail="Failed to fetch updated listener")

    return EventListenerResponse(
        id=listener["id"],
        name=listener["name"],
        source_id=listener["source_id"],
        action_type=listener["action_type"],
        match_conditions=listener["match_conditions"],
        action_config=listener["action_config"],
        description=listener["description"],
        enabled=listener["enabled"],
        one_time=listener["one_time"],
        created_at=listener["created_at"].isoformat(),
        last_execution_at=(
            listener["last_execution_at"].isoformat()
            if listener["last_execution_at"]
            else None
        ),
        daily_executions=listener["daily_executions"],
        conversation_id=listener["conversation_id"],
        interface_type=listener["interface_type"],
        condition_script=listener["condition_script"],
    )


@listeners_api_router.delete("/{listener_id}")
async def delete_event_listener(
    listener_id: int,
    conversation_id: Annotated[
        str, Query(description="Conversation ID for permission check")
    ],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Delete an event listener."""
    # Check if listener exists and user has permission
    listener = await db.events.get_event_listener_by_id(listener_id, conversation_id)
    if not listener:
        raise HTTPException(status_code=404, detail="Event listener not found")

    success = await db.events.delete_event_listener(listener_id, conversation_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete event listener")

    return {"message": f"Event listener '{listener['name']}' deleted successfully"}
