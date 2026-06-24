"""Tools module for the Family Assistant.

This module provides tools that can be used by the LLM to perform various actions.
The tools are organized into thematic submodules for better maintainability.
"""

from __future__ import annotations

from family_assistant import storage
from family_assistant.tools.attachments import (
    ATTACHMENT_TOOLS_DEFINITION,
    attach_to_response_tool,
    read_text_attachment_tool,
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
from family_assistant.tools.browser_dom import (
    BROWSER_DOM_TOOLS_DEFINITION,
    browser_claim_handback_tool,
    browser_click_tool,
    browser_exec_tool,
    browser_extract_tool,
    browser_fill_tool,
    browser_open_tool,
    browser_request_handoff_tool,
    browser_screenshot_tool,
    browser_select_tool,
    browser_snapshot_tool,
    browser_wait_tool,
)
from family_assistant.tools.calendar import (
    CALENDAR_TOOLS_DEFINITION,
    add_calendar_event_tool,
    delete_calendar_event_tool,
    modify_calendar_event_tool,
    search_calendar_events_tool,
)
from family_assistant.tools.camera import (
    CAMERA_TOOLS_DEFINITION,
    get_camera_frame_tool,
    get_camera_frames_batch_tool,
    get_camera_recordings_tool,
    get_live_camera_snapshot_tool,
    list_cameras_tool,
    scan_camera_frames_tool,
    search_camera_events_tool,
)
from family_assistant.tools.communication import (
    COMMUNICATION_TOOLS_DEFINITION,
    get_attachment_info_tool,
    get_message_history_tool,
    send_message_to_user_tool,
)
from family_assistant.tools.computer_use import (
    COMPUTER_USE_TOOLS_DEFINITION,
    computer_use_click_at,
    computer_use_drag_and_drop,
    computer_use_go_back,
    computer_use_go_forward,
    computer_use_hover_at,
    computer_use_key_combination,
    computer_use_navigate,
    computer_use_open_web_browser,
    computer_use_scroll_at,
    computer_use_scroll_document,
    computer_use_search,
    computer_use_type_text_at,
    computer_use_wait_5_seconds,
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
    reindex_email_tool,
    search_documents_tool,
)
from family_assistant.tools.engineering import (
    ENGINEERING_TOOLS_DEFINITION,
    create_github_issue,
    get_llm_request_history,
    get_mcp_server_status,
    get_profile_config,
    get_profile_tool_inventory,
    get_resolved_config,
    get_system_info,
    query_database,
    read_error_logs,
    read_source_file,
    reconnect_mcp_server,
    search_source_code,
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
    call_home_assistant_action_tool,
    download_state_history_tool,
    get_camera_snapshot_tool,
    list_home_assistant_actions_tool,
    list_home_assistant_entities_tool,
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
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    ToolConfirmationFailed,
    ToolConfirmationRequired,
    ToolNotFoundError,
    ToolPolicyDeniedError,
    ToolsProvider,
    collect_system_prompt_addition,
    find_provider_by_type,
    get_tool_definitions_for_advertisement,
)
from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.media_download import (
    MEDIA_DOWNLOAD_TOOLS_DEFINITION,
    download_media_tool,
)
from family_assistant.tools.metadata import (
    LocalToolMetadata,
    ToolDescriptor,
    ToolImplementation,
    ToolRegistration,
    ToolTag,
    build_local_tool_descriptors,
    build_local_tool_registrations,
    make_local_tool_metadata,
)
from family_assistant.tools.mock_image_tools import (
    MOCK_IMAGE_TOOLS_DEFINITION,
    annotate_image_tool,
    mock_camera_snapshot_tool,
)
from family_assistant.tools.mqtt import (
    MQTT_TOOLS_DEFINITION,
    mqtt_publish_tool,
)
from family_assistant.tools.notes import (
    NOTE_TOOLS_DEFINITION,
    add_or_update_note_tool,
    delete_note_tool,
    get_note_tool,
    list_notes_tool,
)
from family_assistant.tools.on_demand import (
    ACTIVATE_TOOLS_DEFINITION,
    OnDemandCatalogEntry,
    OnDemandToolCatalog,
    OnDemandToolsView,
)
from family_assistant.tools.policy import (
    DEFAULT_POLICY_PRIORITY_OFFSET,
    OPERATOR_POLICY_PRIORITY_OFFSET,
    PROFILE_POLICY_PRIORITY_OFFSET,
    PolicyEngine,
    PolicyEvaluation,
    PolicyRule,
    ResolvedPolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.problem_reporting import (
    REPORT_TECHNICAL_PROBLEM_TOOLS_DEFINITION,
    report_technical_problem_tool,
)
from family_assistant.tools.script_testing import (
    SCRIPT_TESTING_TOOLS_DEFINITION,
    test_script_with_simulated_tools_tool,
)
from family_assistant.tools.services import (
    SERVICE_TOOLS_DEFINITION,
    delegate_to_service_tool,
    get_delegation_status_tool,
    list_delegations_tool,
)
from family_assistant.tools.shopping import (
    SHOPPING_TOOLS_DEFINITION,
    shopify_add_to_cart_tool,
    shopify_get_cart_tool,
    shopify_transfer_checkout_to_human_tool,
)
from family_assistant.tools.stored_scripts import (
    STORED_SCRIPTS_TOOLS_DEFINITION,
    delete_script_tool,
    get_script_tool,
    list_scripts_tool,
    save_script_tool,
)
from family_assistant.tools.tasks import (
    TASK_TOOLS_DEFINITION,
    cancel_pending_callback_tool,
    list_pending_callbacks_tool,
    modify_pending_callback_tool,
    schedule_action_tool,
    schedule_future_callback_tool,
    schedule_reminder_tool,
)
from family_assistant.tools.types import (
    MCPServerConfig,
    ToolDefinition,
    ToolExecutionContext,
)
from family_assistant.tools.video_generation import (
    VIDEO_GENERATION_TOOLS_DEFINITION,
    generate_video_tool,
)
from family_assistant.tools.worker import (
    WORKER_TOOLS_DEFINITION,
    cancel_worker_task_tool,
    list_worker_tasks_tool,
    read_task_result_tool,
    spawn_worker_tool,
)
from family_assistant.tools.workspace_files import (
    WORKSPACE_TOOLS_DEFINITION,
    workspace_delete_tool,
    workspace_export_notes_tool,
    workspace_glob_tool,
    workspace_import_note_tool,
    workspace_mkdir_tool,
    workspace_read_tool,
    workspace_write_tool,
)

# Export all public interfaces
__all__ = [
    # From infrastructure
    "ToolsProvider",
    "LocalToolsProvider",
    "CompositeToolsProvider",
    "PolicyEnforcingToolsProvider",
    "MCPServerConfig",
    "collect_system_prompt_addition",
    "find_provider_by_type",
    "get_tool_definitions_for_advertisement",
    "ToolNotFoundError",
    "ToolPolicyDeniedError",
    "ToolConfirmationRequired",
    "ToolConfirmationFailed",
    "ConfirmationCallbackProtocol",
    # From types
    "ToolExecutionContext",
    "ToolTag",
    "LocalToolMetadata",
    "ToolRegistration",
    "ToolDescriptor",
    "ToolMatcher",
    "PolicyRule",
    "ToolPolicyConfig",
    "ToolPolicyDecision",
    "PolicyEvaluation",
    "ResolvedPolicyRule",
    "PolicyEngine",
    "DEFAULT_POLICY_PRIORITY_OFFSET",
    "PROFILE_POLICY_PRIORITY_OFFSET",
    "OPERATOR_POLICY_PRIORITY_OFFSET",
    # Tool definitions
    "TOOLS_DEFINITION",
    "AVAILABLE_FUNCTIONS",
    "LOCAL_TOOL_METADATA_BY_NAME",
    "LOCAL_TOOL_REGISTRATIONS",
    "LOCAL_TOOL_DESCRIPTORS",
    "build_local_tool_registrations",
    # Confirmation renderers
    "TOOL_CONFIRMATION_RENDERERS",
    # Individual tool functions (for testing/direct use)
    "schedule_future_callback_tool",
    "search_documents_tool",
    "get_full_document_content_tool",
    "ingest_document_from_url_tool",
    "get_message_history_tool",
    "list_pending_callbacks_tool",
    "modify_pending_callback_tool",
    "cancel_pending_callback_tool",
    "delegate_to_service_tool",
    "get_delegation_status_tool",
    "list_delegations_tool",
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
    "EVENT_TOOLS_DEFINITION",
    "query_recent_events_tool",
    "test_event_listener_tool",
    "HOME_ASSISTANT_TOOLS_DEFINITION",
    "render_home_assistant_template_tool",
    # Camera tools
    "CAMERA_TOOLS_DEFINITION",
    "list_cameras_tool",
    "search_camera_events_tool",
    "get_camera_frame_tool",
    "get_camera_frames_batch_tool",
    "get_camera_recordings_tool",
    "get_live_camera_snapshot_tool",
    "scan_camera_frames_tool",
    "storage",
    "execute_script_tool",
    "test_script_with_simulated_tools_tool",
    "SCRIPT_TOOLS_DEFINITION",
    "SCRIPT_TESTING_TOOLS_DEFINITION",
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
    "list_home_assistant_entities_tool",
    "list_home_assistant_actions_tool",
    "call_home_assistant_action_tool",
    "DATA_VISUALIZATION_TOOLS_DEFINITION",
    "create_vega_chart_tool",
    "DATA_MANIPULATION_TOOLS_DEFINITION",
    "jq_query_tool",
    "VIDEO_GENERATION_TOOLS_DEFINITION",
    "generate_video_tool",
    "MEDIA_DOWNLOAD_TOOLS_DEFINITION",
    "download_media_tool",
    # MQTT tools
    "MQTT_TOOLS_DEFINITION",
    "mqtt_publish_tool",
    # Shopping/UCP tools
    "SHOPPING_TOOLS_DEFINITION",
    "shopify_add_to_cart_tool",
    "shopify_get_cart_tool",
    "shopify_transfer_checkout_to_human_tool",
    "COMPUTER_USE_TOOLS_DEFINITION",
    "computer_use_click_at",
    "computer_use_drag_and_drop",
    "computer_use_go_back",
    "computer_use_go_forward",
    "computer_use_hover_at",
    "computer_use_key_combination",
    "computer_use_navigate",
    "computer_use_open_web_browser",
    "computer_use_scroll_at",
    "computer_use_scroll_document",
    "computer_use_search",
    "computer_use_type_text_at",
    "computer_use_wait_5_seconds",
    # Browser DOM (semantic) tools
    "BROWSER_DOM_TOOLS_DEFINITION",
    "browser_claim_handback_tool",
    "browser_click_tool",
    "browser_exec_tool",
    "browser_extract_tool",
    "browser_fill_tool",
    "browser_open_tool",
    "browser_request_handoff_tool",
    "browser_screenshot_tool",
    "browser_select_tool",
    "browser_snapshot_tool",
    "browser_wait_tool",
    # Workspace file tools
    "WORKSPACE_TOOLS_DEFINITION",
    "workspace_read_tool",
    "workspace_write_tool",
    "workspace_glob_tool",
    "workspace_delete_tool",
    "workspace_mkdir_tool",
    "workspace_export_notes_tool",
    "workspace_import_note_tool",
    # Worker tools
    "WORKER_TOOLS_DEFINITION",
    "spawn_worker_tool",
    "read_task_result_tool",
    "list_worker_tasks_tool",
    "cancel_worker_task_tool",
    # Engineering tools
    "ENGINEERING_TOOLS_DEFINITION",
    "read_source_file",
    "search_source_code",
    "query_database",
    "read_error_logs",
    "create_github_issue",
    "get_llm_request_history",
    "get_mcp_server_status",
    "reconnect_mcp_server",
    "get_resolved_config",
    "get_profile_config",
    "get_profile_tool_inventory",
    "get_system_info",
    # On-demand tool activation
    "ACTIVATE_TOOLS_DEFINITION",
    "OnDemandCatalogEntry",
    "OnDemandToolCatalog",
    "OnDemandToolsView",
    # Stored scripts
    "STORED_SCRIPTS_TOOLS_DEFINITION",
    "save_script_tool",
    "list_scripts_tool",
    "get_script_tool",
    "delete_script_tool",
    "reindex_email_tool",
    "MCPToolsProvider",
    # Problem reporting
    "REPORT_TECHNICAL_PROBLEM_TOOLS_DEFINITION",
    "report_technical_problem_tool",
]


# IMPORTANT: Tool Registration Process
# ====================================
# To add a new tool to the system, you MUST:
# 1. Add the tool function to the implementation map below
# 2. Add the tool definition to the appropriate TOOLS_DEFINITION list (e.g., NOTE_TOOLS_DEFINITION)
# 3. Add tool metadata in LOCAL_TOOL_METADATA_BY_NAME below
# 4. Add or adjust tools_policy rules for each profile that should have access
#
# The registration-backed catalog provides security and flexibility:
# - Different profiles can have different tool access (e.g., browser profile has only browser tools)
# - Destructive tools can be excluded from certain profiles
# - New profiles can mix and match tools without code changes
#
# Note: Runtime tool access is denied unless tools_policy allows it.


def _metadata(*tags: ToolTag, summary: str | None = None) -> LocalToolMetadata:
    """Create validated metadata for a local tool registration."""
    return make_local_tool_metadata(list(tags), summary=summary)


_LOCAL_TOOL_DEFINITIONS: list[ToolDefinition] = (
    NOTE_TOOLS_DEFINITION
    + SERVICE_TOOLS_DEFINITION
    + TASK_TOOLS_DEFINITION
    + DOCUMENT_TOOLS_DEFINITION
    + EVENT_TOOLS_DEFINITION
    + AUTOMATIONS_TOOLS_DEFINITION
    + HOME_ASSISTANT_TOOLS_DEFINITION
    + CAMERA_TOOLS_DEFINITION
    + CALENDAR_TOOLS_DEFINITION
    + COMMUNICATION_TOOLS_DEFINITION
    + SCRIPT_TOOLS_DEFINITION
    + STORED_SCRIPTS_TOOLS_DEFINITION
    + SCRIPT_TESTING_TOOLS_DEFINITION
    + ATTACHMENT_TOOLS_DEFINITION
    + IMAGE_TOOLS_DEFINITION
    + IMAGE_GENERATION_TOOLS_DEFINITION
    + DATA_VISUALIZATION_TOOLS_DEFINITION
    + DATA_MANIPULATION_TOOLS_DEFINITION
    + VIDEO_GENERATION_TOOLS_DEFINITION
    + MEDIA_DOWNLOAD_TOOLS_DEFINITION
    + MOCK_IMAGE_TOOLS_DEFINITION
    + COMPUTER_USE_TOOLS_DEFINITION
    + BROWSER_DOM_TOOLS_DEFINITION
    + WORKSPACE_TOOLS_DEFINITION
    + WORKER_TOOLS_DEFINITION
    + ENGINEERING_TOOLS_DEFINITION
    + MQTT_TOOLS_DEFINITION
    + SHOPPING_TOOLS_DEFINITION
    + REPORT_TECHNICAL_PROBLEM_TOOLS_DEFINITION
)

_LOCAL_TOOL_IMPLEMENTATIONS: dict[str, ToolImplementation] = {
    "add_or_update_note": add_or_update_note_tool,
    "get_note": get_note_tool,
    "list_notes": list_notes_tool,
    "delete_note": delete_note_tool,
    "schedule_future_callback": schedule_future_callback_tool,
    "schedule_reminder": schedule_reminder_tool,
    "schedule_action": schedule_action_tool,
    "search_documents": search_documents_tool,
    "get_full_document_content": get_full_document_content_tool,
    "get_attachment_info": get_attachment_info_tool,
    "get_message_history": get_message_history_tool,
    "get_user_documentation_content": get_user_documentation_content_tool,
    "ingest_document_from_url": ingest_document_from_url_tool,
    "reindex_email": reindex_email_tool,
    "send_message_to_user": send_message_to_user_tool,
    # Calendar tools
    "add_calendar_event": add_calendar_event_tool,
    "search_calendar_events": search_calendar_events_tool,
    "modify_calendar_event": modify_calendar_event_tool,
    "delete_calendar_event": delete_calendar_event_tool,
    "delegate_to_service": delegate_to_service_tool,
    "get_delegation_status": get_delegation_status_tool,
    "list_delegations": list_delegations_tool,
    "list_pending_callbacks": list_pending_callbacks_tool,
    "modify_pending_callback": modify_pending_callback_tool,
    "cancel_pending_callback": cancel_pending_callback_tool,
    "query_recent_events": query_recent_events_tool,
    "test_event_listener": test_event_listener_tool,
    "render_home_assistant_template": render_home_assistant_template_tool,
    "get_camera_snapshot": get_camera_snapshot_tool,
    "download_state_history": download_state_history_tool,
    "list_home_assistant_entities": list_home_assistant_entities_tool,
    "list_home_assistant_actions": list_home_assistant_actions_tool,
    "call_home_assistant_action": call_home_assistant_action_tool,
    # Camera tools (Reolink/Frigate backend)
    "list_cameras": list_cameras_tool,
    "search_camera_events": search_camera_events_tool,
    "get_camera_frame": get_camera_frame_tool,
    "get_camera_frames_batch": get_camera_frames_batch_tool,
    "get_camera_recordings": get_camera_recordings_tool,
    "get_live_camera_snapshot": get_live_camera_snapshot_tool,
    "scan_camera_frames": scan_camera_frames_tool,
    "execute_script": execute_script_tool,
    # Stored scripts
    "save_script": save_script_tool,
    "list_scripts": list_scripts_tool,
    "get_script": get_script_tool,
    "delete_script": delete_script_tool,
    "test_script_with_simulated_tools": test_script_with_simulated_tools_tool,
    "attach_to_response": attach_to_response_tool,
    "read_text_attachment": read_text_attachment_tool,
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
    # Video generation tools
    "generate_video": generate_video_tool,
    # Media download tools
    "download_media": download_media_tool,
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
    # Computer Use tools
    "click_at": computer_use_click_at,
    "drag_and_drop": computer_use_drag_and_drop,
    "go_back": computer_use_go_back,
    "go_forward": computer_use_go_forward,
    "hover_at": computer_use_hover_at,
    "key_combination": computer_use_key_combination,
    "navigate": computer_use_navigate,
    "open_web_browser": computer_use_open_web_browser,
    "scroll_at": computer_use_scroll_at,
    "scroll_document": computer_use_scroll_document,
    "search": computer_use_search,
    "type_text_at": computer_use_type_text_at,
    "wait_5_seconds": computer_use_wait_5_seconds,
    # Browser DOM (semantic) tools
    "browser_open": browser_open_tool,
    "browser_snapshot": browser_snapshot_tool,
    "browser_click": browser_click_tool,
    "browser_claim_handback": browser_claim_handback_tool,
    "browser_fill": browser_fill_tool,
    "browser_select": browser_select_tool,
    "browser_wait": browser_wait_tool,
    "browser_extract": browser_extract_tool,
    "browser_screenshot": browser_screenshot_tool,
    "browser_exec": browser_exec_tool,
    "browser_request_handoff": browser_request_handoff_tool,
    # Workspace file tools
    "workspace_read": workspace_read_tool,
    "workspace_write": workspace_write_tool,
    "workspace_glob": workspace_glob_tool,
    "workspace_delete": workspace_delete_tool,
    "workspace_mkdir": workspace_mkdir_tool,
    "workspace_export_notes": workspace_export_notes_tool,
    "workspace_import_note": workspace_import_note_tool,
    # Worker tools
    "spawn_worker": spawn_worker_tool,
    "read_task_result": read_task_result_tool,
    "list_worker_tasks": list_worker_tasks_tool,
    "cancel_worker_task": cancel_worker_task_tool,
    # Engineering tools
    "read_source_file": read_source_file,
    "search_source_code": search_source_code,
    "query_database": query_database,
    "read_error_logs": read_error_logs,
    "create_github_issue": create_github_issue,
    "get_llm_request_history": get_llm_request_history,
    "get_mcp_server_status": get_mcp_server_status,
    "reconnect_mcp_server": reconnect_mcp_server,
    "get_resolved_config": get_resolved_config,
    "get_profile_config": get_profile_config,
    "get_profile_tool_inventory": get_profile_tool_inventory,
    "get_system_info": get_system_info,
    # MQTT tools
    "mqtt_publish": mqtt_publish_tool,
    # Shopping/UCP tools
    "shopify_add_to_cart": shopify_add_to_cart_tool,
    "shopify_get_cart": shopify_get_cart_tool,
    "shopify_transfer_checkout_to_human": shopify_transfer_checkout_to_human_tool,
    # Problem reporting
    "report_technical_problem": report_technical_problem_tool,
}

LOCAL_TOOL_METADATA_BY_NAME: dict[str, LocalToolMetadata] = {
    "add_or_update_note": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.NOTES,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_note": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.NOTES,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_notes": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.NOTES,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "delete_note": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.NOTES,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "delegate_to_service": _metadata(
        ToolTag.DELEGATION,
        ToolTag.OUTPUT_UNSPECIFIED,
    ),
    "get_delegation_status": _metadata(
        ToolTag.DELEGATION,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_delegations": _metadata(
        ToolTag.DELEGATION,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "schedule_reminder": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "schedule_future_callback": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_pending_callbacks": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "modify_pending_callback": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "cancel_pending_callback": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "schedule_action": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SCHEDULING,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "search_documents": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DOCUMENTS,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_full_document_content": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DOCUMENTS,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "ingest_document_from_url": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.DOCUMENTS,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "reindex_email": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.DOCUMENTS,
    ),
    "get_user_documentation_content": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.DOCUMENTS,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "query_recent_events": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "test_event_listener": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "create_automation": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_automations": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_automation": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "update_automation": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "enable_automation": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "disable_automation": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "delete_automation": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_automation_stats": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "download_state_history": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "render_home_assistant_template": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_camera_snapshot": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "list_home_assistant_entities": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.HOME_AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_home_assistant_actions": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.HOME_AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "call_home_assistant_action": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.HOME_AUTOMATION,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_cameras": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "search_camera_events": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_camera_frame": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_camera_frames_batch": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_camera_recordings": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_live_camera_snapshot": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "scan_camera_frames": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "add_calendar_event": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CALENDAR,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "search_calendar_events": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CALENDAR,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "modify_calendar_event": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CALENDAR,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "delete_calendar_event": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.CALENDAR,
        ToolTag.SCHEDULING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_message_history": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "send_message_to_user": _metadata(
        ToolTag.EXTERNAL_COMM,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_attachment_info": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "execute_script": _metadata(
        ToolTag.CODE_EXECUTION,
        ToolTag.STATE_CHANGING,
        ToolTag.OUTPUT_UNSPECIFIED,
    ),
    "save_script": _metadata(
        ToolTag.CODE_EXECUTION,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_scripts": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_script": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "delete_script": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "test_script_with_simulated_tools": _metadata(
        ToolTag.CODE_EXECUTION,
        ToolTag.OUTPUT_UNSPECIFIED,
    ),
    "attach_to_response": _metadata(
        ToolTag.EXTERNAL_COMM,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "read_text_attachment": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DOCUMENTS,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "highlight_image": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "generate_image": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "transform_image": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "create_vega_chart": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.DATA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "jq_query": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.DATA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "generate_video": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "download_media": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "mock_camera_snapshot": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.CAMERA,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "annotate_image": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "click_at": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "type_text_at": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "scroll_at": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "open_web_browser": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "navigate": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "search": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "go_back": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "go_forward": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "key_combination": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "wait_5_seconds": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "hover_at": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "drag_and_drop": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "scroll_document": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    # Browser DOM (semantic) tools — same [BC] posture as Computer Use tools:
    # untrusted web output, external comms, and DOM state changes.
    "browser_open": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_snapshot": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_click": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_fill": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_select": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_wait": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_extract": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_screenshot": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_exec": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_request_handoff": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
    ),
    "browser_claim_handback": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
    ),
    "workspace_read": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "workspace_write": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "workspace_glob": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "workspace_delete": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "workspace_mkdir": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.FILE_SYSTEM,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "workspace_export_notes": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.NOTES,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "workspace_import_note": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.NOTES,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "spawn_worker": _metadata(
        ToolTag.CODE_EXECUTION,
        ToolTag.STATE_CHANGING,
        ToolTag.WORKER,
        ToolTag.OUTPUT_UNSPECIFIED,
    ),
    "read_task_result": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.WORKER,
        ToolTag.OUTPUT_UNSPECIFIED,
    ),
    "cancel_worker_task": _metadata(
        ToolTag.DESTRUCTIVE,
        ToolTag.STATE_CHANGING,
        ToolTag.WORKER,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "list_worker_tasks": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.WORKER,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "read_source_file": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.DOCUMENTS,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "search_source_code": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.DOCUMENTS,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "query_database": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "read_error_logs": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.DOCUMENTS,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_llm_request_history": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "create_github_issue": _metadata(
        ToolTag.EXTERNAL_COMM,
        ToolTag.STATE_CHANGING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_mcp_server_status": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "reconnect_mcp_server": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_resolved_config": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_profile_config": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_profile_tool_inventory": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_system_info": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.OUTPUT_TRUSTED,
    ),
    # MQTT tools
    "mqtt_publish": _metadata(
        ToolTag.EXTERNAL_COMM,
        ToolTag.HOME_AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "shopify_add_to_cart": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.SHOPPING,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "shopify_get_cart": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.SHOPPING,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "shopify_transfer_checkout_to_human": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.SHOPPING,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    # Problem reporting
    "report_technical_problem": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.OUTPUT_TRUSTED,
    ),
}

LOCAL_TOOL_REGISTRATIONS: list[ToolRegistration] = build_local_tool_registrations(
    definitions=_LOCAL_TOOL_DEFINITIONS,
    implementations=_LOCAL_TOOL_IMPLEMENTATIONS,
    metadata_by_name=LOCAL_TOOL_METADATA_BY_NAME,
)
LOCAL_TOOL_DESCRIPTORS: list[ToolDescriptor] = build_local_tool_descriptors(
    LOCAL_TOOL_REGISTRATIONS
)

AVAILABLE_FUNCTIONS: dict[str, ToolImplementation] = {
    registration.name: registration.implementation
    for registration in LOCAL_TOOL_REGISTRATIONS
}

TOOLS_DEFINITION: list[ToolDefinition] = [
    registration.definition for registration in LOCAL_TOOL_REGISTRATIONS
]
