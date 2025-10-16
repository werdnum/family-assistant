"""Type definitions for storage layer return types."""

from datetime import datetime
from typing import Any, TypedDict


class EventListenerDict(TypedDict):
    """Type definition for event listener records returned from repository."""

    id: int
    name: str
    description: str | None
    source_id: str  # EventSourceType value
    match_conditions: dict[str, Any]
    action_type: str  # EventActionType value
    action_config: dict[str, Any] | None
    condition_script: str | None
    conversation_id: str
    interface_type: str  # InterfaceType value
    one_time: bool
    enabled: bool
    created_at: datetime
    daily_executions: int
    daily_reset_at: datetime | None
    last_execution_at: datetime | None


class ScheduleAutomationDict(TypedDict):
    """Type definition for schedule automation records returned from repository."""

    id: int
    name: str
    description: str | None
    conversation_id: str
    interface_type: str  # InterfaceType value
    recurrence_rule: str
    next_scheduled_at: datetime | None
    action_type: str  # EventActionType value
    action_config: dict[str, Any]
    enabled: bool
    created_at: datetime
    last_execution_at: datetime | None
    execution_count: int


class RecentEventDict(TypedDict):
    """Type definition for recent event records returned from repository."""

    id: int
    event_id: str
    source_id: str  # EventSourceType value
    event_data: dict[str, Any]
    triggered_listener_ids: list[int] | None
    timestamp: datetime
    created_at: datetime
