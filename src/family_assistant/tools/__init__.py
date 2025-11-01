"""Tools module for the Family Assistant.

This module provides tools that can be used by the LLM to perform various actions.
The tools are organized into thematic submodules for better maintainability.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from family_assistant import storage
from family_assistant.tools.attachments import (
    ATTACHMENT_TOOLS_DEFINITION,
    attach_to_response_tool,
)
from family_assistant.tools.automations import (
    AUTOMATIONS_TOOLS_DEFINITION,
    create_automation_tool,
    delete_automation_tool,
    disable_automation_tool,
    enable_automation_tool,
    get_automation_stats_tool,
    get_automation_tool,
    list_automations_tool,
    update_automation_tool,
)
from family_assistant.tools.calendar import (
    CALENDAR_TOOLS_DEFINITION,
    add_calendar_event_tool,
    delete_calendar_event_tool,
    modify_calendar_event_tool,
    search_calendar_events_tool,
)
from family_assistant.tools.communication import (
    COMMUNICATION_TOOLS_DEFINITION,
    get_attachment_info_tool,
    get_message_history_tool,
    send_message_to_user_tool,
)
from family_assistant.tools.confirmation import (
    TOOL_CONFIRMATION_RENDERERS,
    _format_event_details_for_confirmation,
    render_delete_calendar_event_confirmation,
    render_modify_calendar_event_confirmation,
)
from family_assistant.tools.data_manipulation import (
    DATA_MANIPULATION_TOOLS_DEFINITION,
    jq_query_tool,
)
from family_assistant.tools.data_visualization import (
    DATA_VISUALIZATION_TOOLS_DEFINITION,
    create_vega_chart_tool,
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
    test_event_listener_tool,
)
from family_assistant.tools.execute_script import (
    SCRIPT_TOOLS_DEFINITION,
    execute_script_tool,
)
from family_assistant.tools.home_assistant import (
    HOME_ASSISTANT_TOOLS_DEFINITION,
    download_state_history_tool,
    get_camera_snapshot_tool,
    render_home_assistant_template_tool,
)
from family_assistant.tools.image_generation import (
    IMAGE_GENERATION_TOOLS_DEFINITION,
    generate_image_tool,
    transform_image_tool,
)
from family_assistant.tools.image_tools import (
    IMAGE_TOOLS_DEFINITION,
    highlight_image_tool,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    ConfirmationCallbackProtocol,
    ConfirmingToolsProvider,
    FilteredToolsProvider,
    LocalToolsProvider,
    ToolConfirmationFailed,
    ToolConfirmationRequired,
    ToolNotFoundError,
    ToolsProvider,
)
from family_assistant.tools.mock_image_tools import (
    MOCK_IMAGE_TOOLS_DEFINITION,
    annotate_image_tool,
    mock_camera_snapshot_tool,
)
from family_assistant.tools.notes import (
    NOTE_TOOLS_DEFINITION,
    add_or_update_note_tool,
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
    schedule_action_tool,
    schedule_future_callback_tool,
    schedule_recurring_action_tool,
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
    "FilteredToolsProvider",
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
    "add_or_update_note_tool",
    "delete_note_tool",
    "get_note_tool",
    "list_notes_tool",
    "schedule_reminder_tool",
    "schedule_action_tool",
    "schedule_recurring_action_tool",
    "EVENT_TOOLS_DEFINITION",
    "query_recent_events_tool",
    "test_event_listener_tool",
    "HOME_ASSISTANT_TOOLS_DEFINITION",
    "render_home_assistant_template_tool",
    "storage",
    "execute_script_tool",
    "SCRIPT_TOOLS_DEFINITION",
    "get_camera_snapshot_tool",
    "ATTACHMENT_TOOLS_DEFINITION",
    "attach_to_response_tool",
    "MOCK_IMAGE_TOOLS_DEFINITION",
    "annotate_image_tool",
    "mock_camera_snapshot_tool",
    "get_attachment_info_tool",
    "IMAGE_TOOLS_DEFINITION",
    "highlight_image_tool",
    "IMAGE_GENERATION_TOOLS_DEFINITION",
    "generate_image_tool",
    "transform_image_tool",
    "download_state_history_tool",
    "DATA_VISUALIZATION_TOOLS_DEFINITION",
    "create_vega_chart_tool",
    "DATA_MANIPULATION_TOOLS_DEFINITION",
    "jq_query_tool",
]


# Try to import MCP tools if available
try:
    from family_assistant.tools.mcp import MCPToolsProvider

    __all__.append("MCPToolsProvider")
except ImportError:
    logger.debug("MCP tools not available")
    MCPToolsProvider = None  # type: ignore[assignment,misc]


# IMPORTANT: Tool Registration Process
# ====================================
# To add a new tool to the system, you MUST:
# 1. Add the tool function to AVAILABLE_FUNCTIONS below
# 2. Add the tool definition to the appropriate TOOLS_DEFINITION list (e.g., NOTE_TOOLS_DEFINITION)
# 3. Add the tool name to config.yaml under enable_local_tools for each profile that should have access
#
# The dual registration provides security and flexibility:
# - Different profiles can have different tool access (e.g., browser profile has only browser tools)
# - Destructive tools can be excluded from certain profiles
# - New profiles can mix and match tools without code changes
#
# Note: If enable_local_tools is not specified for a profile, ALL tools are enabled by default.

# Define available functions mapping
AVAILABLE_FUNCTIONS: dict[str, Callable] = {
    "add_or_update_note": add_or_update_note_tool,
    "get_note": get_note_tool,
    "list_notes": list_notes_tool,
    "delete_note": delete_note_tool,
    "schedule_future_callback": schedule_future_callback_tool,
    "schedule_recurring_task": schedule_recurring_task_tool,
    "schedule_reminder": schedule_reminder_tool,
    "schedule_action": schedule_action_tool,
    "schedule_recurring_action": schedule_recurring_action_tool,
    "search_documents": search_documents_tool,
    "get_full_document_content": get_full_document_content_tool,
    "get_attachment_info": get_attachment_info_tool,
    "get_message_history": get_message_history_tool,
    "get_user_documentation_content": get_user_documentation_content_tool,
    "ingest_document_from_url": ingest_document_from_url_tool,
    "send_message_to_user": send_message_to_user_tool,
    # Calendar tools
    "add_calendar_event": add_calendar_event_tool,
    "search_calendar_events": search_calendar_events_tool,
    "modify_calendar_event": modify_calendar_event_tool,
    "delete_calendar_event": delete_calendar_event_tool,
    "delegate_to_service": delegate_to_service_tool,
    "list_pending_callbacks": list_pending_callbacks_tool,
    "modify_pending_callback": modify_pending_callback_tool,
    "cancel_pending_callback": cancel_pending_callback_tool,
    "query_recent_events": query_recent_events_tool,
    "test_event_listener": test_event_listener_tool,
    "render_home_assistant_template": render_home_assistant_template_tool,
    "get_camera_snapshot": get_camera_snapshot_tool,
    "download_state_history": download_state_history_tool,
    "execute_script": execute_script_tool,
    "attach_to_response": attach_to_response_tool,
    # Mock image processing tools (for testing)
    "annotate_image": annotate_image_tool,
    "mock_camera_snapshot": mock_camera_snapshot_tool,
    # Real image processing tools
    "highlight_image": highlight_image_tool,
    # Image generation tools
    "generate_image": generate_image_tool,
    "transform_image": transform_image_tool,
    # Data visualization tools
    "create_vega_chart": create_vega_chart_tool,
    # Data manipulation tools
    "jq_query": jq_query_tool,
    # Automation tools (unified event + schedule)
    "create_automation": create_automation_tool,
    "list_automations": list_automations_tool,
    "get_automation": get_automation_tool,
    "update_automation": update_automation_tool,
    "enable_automation": enable_automation_tool,
    "disable_automation": disable_automation_tool,
    "delete_automation": delete_automation_tool,
    "get_automation_stats": get_automation_stats_tool,
}


# Combine all tool definitions
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
TOOLS_DEFINITION: list[dict[str, Any]] = (
    NOTE_TOOLS_DEFINITION
    + SERVICE_TOOLS_DEFINITION
    + TASK_TOOLS_DEFINITION
    + DOCUMENT_TOOLS_DEFINITION
    + EVENT_TOOLS_DEFINITION
    + AUTOMATIONS_TOOLS_DEFINITION  # Unified automations (event + schedule)
    + HOME_ASSISTANT_TOOLS_DEFINITION
    + CALENDAR_TOOLS_DEFINITION
    + COMMUNICATION_TOOLS_DEFINITION
    + SCRIPT_TOOLS_DEFINITION
    + ATTACHMENT_TOOLS_DEFINITION
    + IMAGE_TOOLS_DEFINITION
    + IMAGE_GENERATION_TOOLS_DEFINITION
    + DATA_VISUALIZATION_TOOLS_DEFINITION
    + DATA_MANIPULATION_TOOLS_DEFINITION
    + MOCK_IMAGE_TOOLS_DEFINITION
)
