"""Type definitions for storage layer return types."""

from datetime import datetime
from typing import Any, TypedDict


class EventListenerDict(TypedDict):
    """Type definition for event listener records returned from repository."""

    id: int
    name: str
    description: str | None
    source_id: str  # EventSourceType value
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    match_conditions: dict[str, Any]
    action_type: str  # EventActionType value
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    event_data: dict[str, Any]
    triggered_listener_ids: list[int] | None
    timestamp: datetime
    created_at: datetime


class TaskDict(TypedDict):
    """Type definition for task records returned from repository."""

    id: int
    task_id: str
    task_type: str
    # ast-grep-ignore: no-dict-any - Payload is unstructured JSON
    payload: dict[str, Any] | None
    scheduled_at: datetime | None
    created_at: datetime
    status: str
    locked_by: str | None
    locked_at: datetime | None
    error: str | None
    retry_count: int
    max_retries: int
    recurrence_rule: str | None
    original_task_id: str | None
