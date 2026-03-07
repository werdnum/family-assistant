"""Type definitions for storage layer return types."""

from datetime import datetime
from typing import Any, TypedDict

MatchConditions = dict[str, str | int | float | bool]


class ActionConfig(TypedDict, total=False):
    """Action config for wake_llm or script action types.

    Fields are all optional since different action types use different subsets.
    """

    # wake_llm fields
    context: str
    include_event_data: bool
    # script fields
    script_code: str
    timeout: int
    task_name: str


class EventConditionEvaluatorConfig(TypedDict, total=False):
    """Config dict passed to EventConditionEvaluator and EventConditionValidator."""

    script_execution_timeout_ms: int
    script_size_limit_bytes: int


class EventListenerDict(TypedDict):
    """Type definition for event listener records returned from repository."""

    id: int
    name: str
    description: str | None
    source_id: str  # EventSourceType value
    match_conditions: MatchConditions
    action_type: str  # EventActionType value
    action_config: ActionConfig | None
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
    action_config: ActionConfig
    enabled: bool
    created_at: datetime
    last_execution_at: datetime | None
    execution_count: int


class RecentEventDict(TypedDict):
    """Type definition for recent event records returned from repository."""

    id: int
    event_id: str
    source_id: str  # EventSourceType value
    # ast-grep-ignore: no-dict-any - event_data contains arbitrary JSON from external sources (Home Assistant, webhooks) with no fixed schema
    event_data: dict[str, Any]
    triggered_listener_ids: list[int] | None
    timestamp: datetime
    created_at: datetime


class TaskDict(TypedDict):
    """Type definition for task records returned from repository."""

    id: int
    task_id: str
    task_type: str
    # ast-grep-ignore: no-dict-any - payload is unstructured JSON from multiple task types
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
