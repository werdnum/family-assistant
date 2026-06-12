"""Type definitions for storage layer return types."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from family_assistant.llm.google_types import GeminiProviderMetadata
    from family_assistant.llm.messages import (
        MessageAttachmentMetadata,
        MessageReasoningInfo,
        ProviderMetadataDict,
    )
    from family_assistant.llm.tool_call import ToolCallItem

MatchConditions = dict[str, str | int | float | bool]


class ActionConfig(TypedDict, total=False):
    """Action config for wake_llm or script action types.

    Fields are all optional since different action types use different subsets.
    """

    # wake_llm fields
    context: str
    include_event_data: bool
    # script fields (inline)
    script_code: str
    # script fields (stored script reference)
    script_name: str
    # ast-grep-ignore: no-dict-any - arbitrary user-defined parameters for stored scripts
    parameters: dict[str, Any]
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
    processing_profile_id: str | None
    created_by_user_id: str | None
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
    processing_profile_id: str | None
    created_by_user_id: str | None
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


class ErrorLogRow(TypedDict):
    """Type definition for error log records returned from repository."""

    id: int
    timestamp: datetime
    logger_name: str
    level: str
    message: str
    exception_type: str | None
    exception_message: str | None
    traceback: str | None
    module: str | None
    function_name: str | None
    # ast-grep-ignore: no-dict-any - extra_data is freeform JSON metadata from logging context
    extra_data: dict[str, Any] | None


class ListenerExecutionStatsDict(TypedDict):
    """Type definition for listener execution statistics."""

    total_executions: int
    daily_executions: int
    daily_limit: int
    last_execution_at: datetime | None
    recent_events: list[RecentEventDict]


class ScheduleExecutionStatsDict(TypedDict):
    """Type definition for schedule automation execution statistics."""

    total_executions: int
    last_execution_at: datetime | None
    next_scheduled_at: datetime | None
    # ast-grep-ignore: no-dict-any - recent task execution rows have dynamic fields from worker_tasks table
    recent_executions: list[dict[str, Any]]


class MessageHistoryRow(TypedDict):
    """Type definition for deserialized message history records.

    Represents a message after JSON fields have been deserialized by
    _process_message_row / _process_message_row_as_dict.
    """

    internal_id: int
    interface_type: str
    conversation_id: str
    interface_message_id: str | None
    turn_id: str | None
    thread_root_id: int | None
    timestamp: datetime
    role: str
    content: str | None
    tool_calls: "list[ToolCallItem] | None"
    reasoning_info: "MessageReasoningInfo | None"
    tool_call_id: str | None
    error_traceback: str | None
    processing_profile_id: str | None
    subconversation_id: str | None
    user_id: str | None
    attachments: "list[MessageAttachmentMetadata] | None"
    tool_name: str | None
    provider_metadata: "ProviderMetadataDict | GeminiProviderMetadata | None"


class ConversationSummaryRow(TypedDict):
    """Type definition for conversation summary records from get_conversation_summaries."""

    conversation_id: str
    last_message: str
    last_timestamp: datetime
    message_count: int
    interface_type: str
