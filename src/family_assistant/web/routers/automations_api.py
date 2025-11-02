"""API endpoints for unified automations management (event + schedule)."""

import logging
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.models import Automation
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)

# Sentinel value to distinguish "not provided" from "explicitly null"
_UNSET = object()

automations_api_router = APIRouter(tags=["Automations"])


class AutomationResponse(BaseModel):
    """Response model for automation data."""

    id: int
    type: str  # "event" or "schedule"
    name: str
    description: str | None
    conversation_id: str
    interface_type: str
    action_type: str
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    action_config: dict[str, Any]
    enabled: bool
    created_at: datetime
    last_execution_at: datetime | None

    # Event-specific fields (null for schedule automations)
    source_id: str | None = None
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    match_conditions: dict[str, Any] | None = None
    condition_script: str | None = None
    one_time: bool | None = None
    daily_executions: int | None = None

    # Schedule-specific fields (null for event automations)
    recurrence_rule: str | None = None
    next_scheduled_at: datetime | None = None
    execution_count: int | None = None


class AutomationsListResponse(BaseModel):
    """Response model for list of automations."""

    automations: list[AutomationResponse]
    total_count: int
    page: int
    page_size: int


class CreateEventAutomationRequest(BaseModel):
    """Request model for creating an event automation."""

    name: str = Field(..., description="Unique name for the automation")
    source_id: str = Field(
        ..., description="Event source: home_assistant, indexing, or webhook"
    )
    action_type: str = Field(..., description="Action type: wake_llm or script")
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    match_conditions: dict[str, Any] = Field(
        ..., description="Conditions to match events"
    )
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    action_config: dict[str, Any] = Field(
        default_factory=dict, description="Configuration for the action"
    )
    description: str | None = Field(None, description="Optional description")
    enabled: bool = Field(True, description="Whether the automation is enabled")
    one_time: bool = Field(False, description="Auto-disable after first trigger")
    conversation_id: str = Field(..., description="Conversation ID for the automation")
    condition_script: str | None = Field(
        None,
        description="Optional Starlark script for condition matching (executed in sandboxed environment)",
    )


class CreateScheduleAutomationRequest(BaseModel):
    """Request model for creating a schedule automation."""

    name: str = Field(..., description="Unique name for the automation")
    recurrence_rule: str = Field(..., description="RRULE string defining the schedule")
    action_type: str = Field(..., description="Action type: wake_llm or script")
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    action_config: dict[str, Any] = Field(
        default_factory=dict, description="Configuration for the action"
    )
    description: str | None = Field(None, description="Optional description")
    enabled: bool = Field(True, description="Whether the automation is enabled")
    conversation_id: str = Field(..., description="Conversation ID for the automation")


class UpdateEventAutomationRequest(BaseModel):
    """Request model for updating an event automation."""

    name: str | None | object = _UNSET
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    match_conditions: dict[str, Any] | None | object = _UNSET
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    action_config: dict[str, Any] | None | object = _UNSET
    description: str | None | object = _UNSET
    enabled: bool | None | object = _UNSET
    one_time: bool | None | object = _UNSET
    condition_script: str | None | object = _UNSET


class UpdateScheduleAutomationRequest(BaseModel):
    """Request model for updating a schedule automation."""

    name: str | None | object = _UNSET
    recurrence_rule: str | None | object = _UNSET
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    action_config: dict[str, Any] | None | object = _UNSET
    description: str | None | object = _UNSET
    enabled: bool | None | object = _UNSET


def _format_automation_response(automation: Automation) -> AutomationResponse:
    """Format automation model into response model."""
    return AutomationResponse(
        id=automation.id,
        type=automation.type,
        name=automation.name,
        description=automation.description,
        conversation_id=automation.conversation_id,
        interface_type=automation.interface_type,
        action_type=automation.action_type,
        action_config=automation.action_config,
        enabled=automation.enabled,
        created_at=automation.created_at,
        last_execution_at=automation.last_execution_at,
        source_id=automation.source_id,
        match_conditions=automation.match_conditions,
        condition_script=automation.condition_script,
        one_time=automation.one_time,
        daily_executions=automation.daily_executions,
        recurrence_rule=automation.recurrence_rule,
        next_scheduled_at=automation.next_scheduled_at,
        execution_count=automation.execution_count,
    )


@automations_api_router.get("")
async def list_automations(
    db: Annotated[DatabaseContext, Depends(get_db)],
    conversation_id: Annotated[
        str | None,
        Query(
            description="Conversation ID to filter by (optional, returns all if not provided)"
        ),
    ] = None,
    automation_type: Annotated[
        str | None, Query(description="Filter by automation type (event or schedule)")
    ] = None,
    enabled: Annotated[
        bool | None, Query(description="Filter by enabled status")
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AutomationsListResponse:
    """List automations with optional filters."""
    # Validate automation_type if provided
    if automation_type and automation_type not in {"event", "schedule"}:
        raise HTTPException(
            status_code=400,
            detail="automation_type must be 'event' or 'schedule'",
        )

    # Fetch automations with pagination
    offset_value = (page - 1) * page_size
    automations, total_count = await db.automations.list_all(
        conversation_id=conversation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        enabled=enabled,
        limit=page_size,
        offset=offset_value,
    )

    # Format automations for response
    formatted_automations = [
        _format_automation_response(automation) for automation in automations
    ]

    return AutomationsListResponse(
        automations=formatted_automations,
        total_count=total_count,
        page=page,
        page_size=page_size,
    )


@automations_api_router.get("/{automation_type}/{automation_id}")
async def get_automation(
    automation_type: str,
    automation_id: int,
    db: Annotated[DatabaseContext, Depends(get_db)],
    conversation_id: Annotated[
        str | None, Query(description="Conversation ID for permission check")
    ] = None,
) -> AutomationResponse:
    """Get a specific automation by type and ID."""
    # Validate automation_type
    if automation_type not in {"event", "schedule"}:
        raise HTTPException(
            status_code=400,
            detail="automation_type must be 'event' or 'schedule'",
        )

    automation = await db.automations.get_by_id(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        conversation_id=conversation_id,
    )

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    return _format_automation_response(automation)


@automations_api_router.post("/event")
async def create_event_automation(
    request: CreateEventAutomationRequest,
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> AutomationResponse:
    """Create a new event automation."""
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

    # Check name uniqueness
    is_available, error_msg = await db.automations.check_name_available(
        name=request.name,
        conversation_id=request.conversation_id,
    )
    if not is_available:
        raise HTTPException(status_code=400, detail=error_msg)

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

        # Fetch the created automation
        automation = await db.automations.get_by_id(
            automation_id=listener_id,
            automation_type="event",
        )
        if not automation:
            raise HTTPException(
                status_code=500, detail="Failed to fetch created automation"
            )

    except Exception as e:
        error_str = str(e)
        if "UNIQUE constraint failed" in error_str and "name" in error_str:
            raise HTTPException(
                status_code=400,
                detail=f"An automation named '{request.name}' already exists in this conversation",
            ) from e
        logger.error(f"Error creating event automation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to create automation: {str(e)}"
        ) from e

    return _format_automation_response(automation)


@automations_api_router.post("/schedule")
async def create_schedule_automation(
    request: CreateScheduleAutomationRequest,
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> AutomationResponse:
    """Create a new schedule automation."""
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

    # Check name uniqueness
    is_available, error_msg = await db.automations.check_name_available(
        name=request.name,
        conversation_id=request.conversation_id,
    )
    if not is_available:
        raise HTTPException(status_code=400, detail=error_msg)

    try:
        automation_id = await db.schedule_automations.create(
            name=request.name,
            recurrence_rule=request.recurrence_rule,
            conversation_id=request.conversation_id,
            interface_type="web",
            action_type=request.action_type,
            action_config=request.action_config,
            description=request.description,
            enabled=request.enabled,
        )

        # Fetch the created automation
        automation = await db.automations.get_by_id(
            automation_id=automation_id,
            automation_type="schedule",
        )
        if not automation:
            raise HTTPException(
                status_code=500, detail="Failed to fetch created automation"
            )

    except Exception as e:
        error_str = str(e)
        if "UNIQUE constraint failed" in error_str and "name" in error_str:
            raise HTTPException(
                status_code=400,
                detail=f"An automation named '{request.name}' already exists in this conversation",
            ) from e
        logger.error(f"Error creating schedule automation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to create automation: {str(e)}"
        ) from e

    return _format_automation_response(automation)


@automations_api_router.patch("/{automation_type}/{automation_id}")
async def update_automation(
    automation_type: str,
    automation_id: int,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    request_body: Annotated[dict[str, Any], Body(...)],
    db: Annotated[DatabaseContext, Depends(get_db)],
    conversation_id: Annotated[
        str | None, Query(description="Conversation ID for permission check")
    ] = None,
) -> AutomationResponse:
    """Update an existing automation."""
    # Validate automation_type
    if automation_type not in {"event", "schedule"}:
        raise HTTPException(
            status_code=400,
            detail="automation_type must be 'event' or 'schedule'",
        )

    # Parse request body into appropriate model based on automation_type
    try:
        if automation_type == "event":
            request = UpdateEventAutomationRequest(**request_body)
        else:
            request = UpdateScheduleAutomationRequest(**request_body)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid request body: {str(e)}",
        ) from e

    # Check if automation exists and belongs to the conversation
    existing = await db.automations.get_by_id(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        conversation_id=conversation_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Verify conversation_id matches for security (if provided)
    if conversation_id is not None and existing.conversation_id != conversation_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Check name uniqueness if name is being changed
    if (
        request.name is not _UNSET
        and request.name is not None
        and request.name != existing.name
    ):
        is_available, error_msg = await db.automations.check_name_available(
            name=cast("str", request.name),
            conversation_id=existing.conversation_id,  # Use existing conversation_id
            exclude_id=automation_id,
            exclude_type=automation_type,  # type: ignore[arg-type]
        )
        if not is_available:
            raise HTTPException(status_code=400, detail=error_msg)

    try:
        if automation_type == "event":
            # Update event automation
            if not isinstance(request, UpdateEventAutomationRequest):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid request body for event automation update",
                )

            success = await db.events.update_event_listener(
                listener_id=automation_id,
                conversation_id=existing.conversation_id,
                name=cast(
                    "str",
                    request.name if request.name is not _UNSET else existing.name,
                ),
                description=cast(
                    "str | None",
                    request.description
                    if request.description is not _UNSET
                    else existing.description,
                ),
                match_conditions=cast(
                    "dict[str, Any]",
                    request.match_conditions
                    if request.match_conditions is not _UNSET
                    else existing.match_conditions,
                ),
                action_config=cast(
                    "dict[str, Any] | None",
                    request.action_config
                    if request.action_config is not _UNSET
                    else existing.action_config,
                ),
                one_time=cast(
                    "bool",
                    request.one_time
                    if request.one_time is not _UNSET
                    else existing.one_time,
                ),
                enabled=cast(
                    "bool",
                    request.enabled
                    if request.enabled is not _UNSET
                    else existing.enabled,
                ),
                condition_script=cast(
                    "str | None",
                    request.condition_script
                    if request.condition_script is not _UNSET
                    else existing.condition_script,
                ),
            )
        else:  # schedule
            # Update schedule automation
            if not isinstance(request, UpdateScheduleAutomationRequest):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid request body for schedule automation update",
                )

            success = await db.schedule_automations.update(
                automation_id=automation_id,
                conversation_id=existing.conversation_id,
                name=request.name,  # Pass _UNSET through for proper sentinel handling
                description=request.description,
                recurrence_rule=request.recurrence_rule,
                action_config=request.action_config,
                enabled=request.enabled,
            )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to update automation")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating automation: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to update automation: {str(e)}"
        ) from e

    # Fetch the updated automation
    automation = await db.automations.get_by_id(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
    )
    if not automation:
        raise HTTPException(
            status_code=500, detail="Failed to fetch updated automation"
        )

    return _format_automation_response(automation)


@automations_api_router.patch("/{automation_type}/{automation_id}/enabled")
async def update_automation_enabled(
    automation_type: str,
    automation_id: int,
    enabled: Annotated[bool, Query(description="New enabled status")],
    conversation_id: Annotated[
        str, Query(description="Conversation ID for permission check")
    ],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> AutomationResponse:
    """Enable or disable an automation."""
    # Validate automation_type
    if automation_type not in {"event", "schedule"}:
        raise HTTPException(
            status_code=400,
            detail="automation_type must be 'event' or 'schedule'",
        )

    success = await db.automations.update_enabled(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        conversation_id=conversation_id,
        enabled=enabled,
    )

    if not success:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Fetch the updated automation
    automation = await db.automations.get_by_id(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
    )
    if not automation:
        raise HTTPException(
            status_code=500, detail="Failed to fetch updated automation"
        )

    return _format_automation_response(automation)


@automations_api_router.delete("/{automation_type}/{automation_id}")
async def delete_automation(
    automation_type: str,
    automation_id: int,
    conversation_id: Annotated[
        str, Query(description="Conversation ID for permission check")
    ],
    db: Annotated[DatabaseContext, Depends(get_db)],
) -> dict[str, str]:
    """Delete an automation."""
    # Validate automation_type
    if automation_type not in {"event", "schedule"}:
        raise HTTPException(
            status_code=400,
            detail="automation_type must be 'event' or 'schedule'",
        )

    # Check if automation exists and belongs to the conversation
    automation = await db.automations.get_by_id(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        conversation_id=conversation_id,
    )
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Verify conversation_id matches for security
    if automation.conversation_id != conversation_id:
        raise HTTPException(status_code=403, detail="Access denied")

    success = await db.automations.delete(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        conversation_id=conversation_id,
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete automation")

    return {"message": f"Automation '{automation.name}' deleted successfully"}


@automations_api_router.get("/{automation_type}/{automation_id}/stats")
async def get_automation_stats(
    automation_type: str,
    automation_id: int,
    conversation_id: Annotated[
        str, Query(description="Conversation ID for permission check")
    ],
    db: Annotated[DatabaseContext, Depends(get_db)],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
) -> dict[str, Any]:
    """Get execution statistics for an automation."""
    # Validate automation_type
    if automation_type not in {"event", "schedule"}:
        raise HTTPException(
            status_code=400,
            detail="automation_type must be 'event' or 'schedule'",
        )

    # Check if automation exists and belongs to the conversation
    automation = await db.automations.get_by_id(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
        conversation_id=conversation_id,
    )
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Verify conversation_id matches for security
    if automation.conversation_id != conversation_id:
        raise HTTPException(status_code=403, detail="Access denied")

    stats = await db.automations.get_execution_stats(
        automation_id=automation_id,
        automation_type=automation_type,  # type: ignore[arg-type]
    )

    return stats
