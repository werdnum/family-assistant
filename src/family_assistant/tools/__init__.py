"""Tools module for the Family Assistant.

This module provides tools that can be used by the LLM to perform various actions.
The tools are organized into thematic submodules for better maintainability.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant import calendar_integration, storage
from family_assistant.tools.communication import (
    COMMUNICATION_TOOLS_DEFINITION,
    get_message_history_tool,
    send_message_to_user_tool,
)
from family_assistant.tools.confirmation import (
    TOOL_CONFIRMATION_RENDERERS,
    _format_event_details_for_confirmation,
    render_delete_calendar_event_confirmation,
    render_modify_calendar_event_confirmation,
)
from family_assistant.tools.documents import (
    DOCUMENT_TOOLS_DEFINITION,
    _scan_user_docs,
    get_full_document_content_tool,
    get_user_documentation_content_tool,
    ingest_document_from_url_tool,
    search_documents_tool,
)
from family_assistant.tools.events import (
    EVENT_TOOLS_DEFINITION,
    query_recent_events_tool,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    ConfirmationCallbackProtocol,
    ConfirmingToolsProvider,
    LocalToolsProvider,
    ToolConfirmationFailed,
    ToolConfirmationRequired,
    ToolNotFoundError,
    ToolsProvider,
)
from family_assistant.tools.notes import (
    NOTE_TOOLS_DEFINITION,
    delete_note_tool,
    get_note_tool,
    list_notes_tool,
)
from family_assistant.tools.services import (
    SERVICE_TOOLS_DEFINITION,
    delegate_to_service_tool,
)
from family_assistant.tools.tasks import (
    TASK_TOOLS_DEFINITION,
    cancel_pending_callback_tool,
    list_pending_callbacks_tool,
    modify_pending_callback_tool,
    schedule_future_callback_tool,
    schedule_recurring_task_tool,
    schedule_reminder_tool,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# Export all public interfaces
__all__ = [
    # From infrastructure
    "ToolsProvider",
    "LocalToolsProvider",
    "CompositeToolsProvider",
    "ConfirmingToolsProvider",
    "ToolNotFoundError",
    "ToolConfirmationRequired",
    "ToolConfirmationFailed",
    "ConfirmationCallbackProtocol",
    # From types
    "ToolExecutionContext",
    # Tool definitions
    "TOOLS_DEFINITION",
    "AVAILABLE_FUNCTIONS",
    # Confirmation renderers
    "TOOL_CONFIRMATION_RENDERERS",
    # Individual tool functions (for testing/direct use)
    "schedule_recurring_task_tool",
    "schedule_future_callback_tool",
    "search_documents_tool",
    "get_full_document_content_tool",
    "ingest_document_from_url_tool",
    "get_message_history_tool",
    "list_pending_callbacks_tool",
    "modify_pending_callback_tool",
    "cancel_pending_callback_tool",
    "delegate_to_service_tool",
    "send_message_to_user_tool",
    "get_user_documentation_content_tool",
    # Helper functions
    "_scan_user_docs",
    "_format_event_details_for_confirmation",
    "render_delete_calendar_event_confirmation",
    "render_modify_calendar_event_confirmation",
    "delete_note_tool",
    "get_note_tool",
    "list_notes_tool",
    "schedule_reminder_tool",
    "EVENT_TOOLS_DEFINITION",
    "query_recent_events_tool",
]


# Try to import MCP tools if available
try:
    from family_assistant.tools.mcp import MCPToolsProvider

    __all__.append("MCPToolsProvider")
except ImportError:
    logger.debug("MCP tools not available")
    MCPToolsProvider = None  # type: ignore[assignment,misc]


# Define available functions mapping
AVAILABLE_FUNCTIONS: dict[str, Callable] = {
    "add_or_update_note": storage.add_or_update_note,
    "get_note": get_note_tool,
    "list_notes": list_notes_tool,
    "delete_note": delete_note_tool,
    "schedule_future_callback": schedule_future_callback_tool,
    "schedule_recurring_task": schedule_recurring_task_tool,
    "schedule_reminder": schedule_reminder_tool,
    "search_documents": search_documents_tool,
    "get_full_document_content": get_full_document_content_tool,
    "get_message_history": get_message_history_tool,
    "get_user_documentation_content": get_user_documentation_content_tool,
    "ingest_document_from_url": ingest_document_from_url_tool,
    "send_message_to_user": send_message_to_user_tool,
    # Calendar tools imported from calendar_integration module
    "add_calendar_event": calendar_integration.add_calendar_event_tool,
    "search_calendar_events": calendar_integration.search_calendar_events_tool,
    "modify_calendar_event": calendar_integration.modify_calendar_event_tool,
    "delete_calendar_event": calendar_integration.delete_calendar_event_tool,
    "delegate_to_service": delegate_to_service_tool,
    "list_pending_callbacks": list_pending_callbacks_tool,
    "modify_pending_callback": modify_pending_callback_tool,
    "cancel_pending_callback": cancel_pending_callback_tool,
    "query_recent_events": query_recent_events_tool,
}


# Combine all tool definitions
TOOLS_DEFINITION: list[dict[str, Any]] = (
    NOTE_TOOLS_DEFINITION
    + SERVICE_TOOLS_DEFINITION
    + TASK_TOOLS_DEFINITION
    + DOCUMENT_TOOLS_DEFINITION
    + EVENT_TOOLS_DEFINITION
    + [
        {
            "type": "function",
            "function": {
                "name": "add_calendar_event",
                "description": (
                    "Adds a new event to the primary family calendar (requires CalDAV configuration). Can create single or recurring events. Use this to schedule appointments, reminders with duration, or block out time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "The title or brief summary of the event.",
                        },
                        "start_time": {
                            "type": "string",
                            "description": (
                                "The start date or datetime of the event in ISO 8601 format. MUST include timezone offset (e.g., '2025-05-20T09:00:00+02:00' for timed event, '2025-05-21' for all-day)."
                            ),
                        },
                        "end_time": {
                            "type": "string",
                            "description": (
                                "The end date or datetime of the event in ISO 8601 format. MUST include timezone offset (e.g., '2025-05-20T10:30:00+02:00' for timed event, '2025-05-22' for all-day - note: end date is exclusive for all-day)."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "Optional. A more detailed description or notes for the event."
                            ),
                        },
                        "all_day": {
                            "type": "boolean",
                            "description": (
                                "Set to true if this is an all-day event, false or omit if it has specific start/end times. Determines if start/end times are treated as dates or datetimes."
                            ),
                            "default": False,
                        },
                        "recurrence_rule": {
                            "type": "string",
                            "description": (
                                "Optional. An RRULE string (RFC 5545) to make this a recurring event (e.g., 'FREQ=WEEKLY;BYDAY=MO;UNTIL=20251231T235959Z'). If omitted, the event is a single instance."
                            ),
                        },
                    },
                    "required": ["summary", "start_time", "end_time"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_calendar_events",
                "description": (
                    "Search for calendar events based on a query and optional date range. Returns a list of matching events with their details and unique IDs (UIDs). Use this *first* when a user asks to modify or delete an event, to identify the correct event UID."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query_text": {
                            "type": "string",
                            "description": (
                                "Keywords from the user's request describing the event (e.g., 'dentist appointment', 'team meeting')."
                            ),
                        },
                        "start_date_str": {
                            "type": "string",
                            "description": (
                                "Optional. The start date for the search range (ISO 8601 format, e.g., '2025-05-20'). Defaults to today if omitted."
                            ),
                        },
                        "end_date_str": {
                            "type": "string",
                            "description": (
                                "Optional. The end date for the search range (ISO 8601 format, e.g., '2025-05-22'). Defaults to start_date + 2 days if omitted."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Optional. Maximum number of events to return (default: 5)."
                            ),
                            "default": 5,
                        },
                    },
                    "required": ["query_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "modify_calendar_event",
                "description": (
                    "Modifies an existing calendar event identified by its UID. Requires the UID obtained from search_calendar_events. Only provide parameters for the fields that need changing. Does *not* currently support modifying recurring events reliably (may affect only the specified instance)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uid": {
                            "type": "string",
                            "description": (
                                "The unique ID (UID) of the event to modify, obtained from search_calendar_events."
                            ),
                        },
                        "calendar_url": {
                            "type": "string",
                            "format": "uri",
                            "description": (
                                "The URL of the calendar containing the event, obtained from search_calendar_events."
                            ),
                        },
                        "new_summary": {
                            "type": "string",
                            "description": "Optional. The new title/summary for the event.",
                        },
                        "new_start_time": {
                            "type": "string",
                            "description": (
                                "Optional. The new start date or datetime (ISO 8601 format with timezone for timed events, e.g., '2025-05-20T11:00:00+02:00' or '2025-05-21')."
                            ),
                        },
                        "new_end_time": {
                            "type": "string",
                            "description": (
                                "Optional. The new end date or datetime (ISO 8601 format with timezone for timed events, e.g., '2025-05-20T11:30:00+02:00' or '2025-05-22')."
                            ),
                        },
                        "new_description": {
                            "type": "string",
                            "description": (
                                "Optional. The new detailed description for the event."
                            ),
                        },
                        "new_all_day": {
                            "type": "boolean",
                            "description": (
                                "Optional. Set to true if the event should become an all-day event, false if it should become timed. Requires appropriate new_start/end_time."
                            ),
                        },
                    },
                    "required": [  # TODO: Logically, at least one 'new_' field is needed, but schema doesn't enforce
                        "uid",
                        "calendar_url",
                    ],  # Require UID and URL, at least one 'new_' field should be provided logically
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_calendar_event",
                "description": (
                    "Deletes a specific calendar event identified by its UID. Requires the UID obtained from search_calendar_events. Does *not* currently support deleting recurring events reliably (may affect only the specified instance)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uid": {
                            "type": "string",
                            "description": (
                                "The unique ID (UID) of the event to delete, obtained from search_calendar_events."
                            ),
                        },
                        "calendar_url": {
                            "type": "string",
                            "format": "uri",
                            "description": (
                                "The URL of the calendar containing the event, obtained from search_calendar_events."
                            ),
                        },
                    },
                    "required": ["uid", "calendar_url"],
                },
            },
        },
    ]
    + COMMUNICATION_TOOLS_DEFINITION
)
