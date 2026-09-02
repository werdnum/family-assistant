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
    computer_use_click,
    computer_use_double_click,
    computer_use_drag_and_drop,
    computer_use_go_back,
    computer_use_go_forward,
    computer_use_hotkey,
    computer_use_key_down,
    computer_use_key_up,
    computer_use_middle_click,
    computer_use_mouse_down,
    computer_use_mouse_up,
    computer_use_move,
    computer_use_navigate,
    computer_use_press_key,
    computer_use_right_click,
    computer_use_scroll,
    computer_use_take_screenshot,
    computer_use_triple_click,
    computer_use_type,
    computer_use_wait,
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
    read_frontend_telemetry,
    read_source_file,
    reconnect_mcp_server,
    resolve_tool_policy,
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
from family_assistant.tools.google_data import (
    GOOGLE_DATA_TOOLS_DEFINITION,
    drive_get_file_tool,
    drive_search_tool,
    drive_write_file_tool,
    gmail_create_draft_tool,
    gmail_get_attachment_tool,
    gmail_get_message_tool,
    gmail_search_tool,
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
    TaintTrackingToolsProvider,
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
    MAX_POLICY_RULE_PRIORITY,
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
    ucp_add_to_cart_tool,
    ucp_get_cart_tool,
    ucp_transfer_checkout_to_human_tool,
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
    # On-demand tool activation
    "ACTIVATE_TOOLS_DEFINITION",
    "ATTACHMENT_TOOLS_DEFINITION",
    "AVAILABLE_FUNCTIONS",
    # Browser DOM (semantic) tools
    "BROWSER_DOM_TOOLS_DEFINITION",
    # Camera tools
    "CAMERA_TOOLS_DEFINITION",
    "COMPUTER_USE_TOOLS_DEFINITION",
    "DATA_MANIPULATION_TOOLS_DEFINITION",
    "DATA_VISUALIZATION_TOOLS_DEFINITION",
    "DEFAULT_POLICY_PRIORITY_OFFSET",
    # Engineering tools
    "ENGINEERING_TOOLS_DEFINITION",
    "EVENT_TOOLS_DEFINITION",
    # Google personal data (Gmail/Drive) tools
    "GOOGLE_DATA_TOOLS_DEFINITION",
    "HOME_ASSISTANT_TOOLS_DEFINITION",
    "IMAGE_GENERATION_TOOLS_DEFINITION",
    "IMAGE_TOOLS_DEFINITION",
    "LOCAL_TOOL_DESCRIPTORS",
    "LOCAL_TOOL_METADATA_BY_NAME",
    "LOCAL_TOOL_REGISTRATIONS",
    "MAX_POLICY_RULE_PRIORITY",
    "MEDIA_DOWNLOAD_TOOLS_DEFINITION",
    "MOCK_IMAGE_TOOLS_DEFINITION",
    # MQTT tools
    "MQTT_TOOLS_DEFINITION",
    "OPERATOR_POLICY_PRIORITY_OFFSET",
    "PROFILE_POLICY_PRIORITY_OFFSET",
    # Problem reporting
    "REPORT_TECHNICAL_PROBLEM_TOOLS_DEFINITION",
    "SCRIPT_TESTING_TOOLS_DEFINITION",
    "SCRIPT_TOOLS_DEFINITION",
    # Shopping/UCP tools
    "SHOPPING_TOOLS_DEFINITION",
    # Stored scripts
    "STORED_SCRIPTS_TOOLS_DEFINITION",
    # Tool definitions
    "TOOLS_DEFINITION",
    # Confirmation renderers
    "TOOL_CONFIRMATION_RENDERERS",
    "VIDEO_GENERATION_TOOLS_DEFINITION",
    # Worker tools
    "WORKER_TOOLS_DEFINITION",
    # Workspace file tools
    "WORKSPACE_TOOLS_DEFINITION",
    "CompositeToolsProvider",
    "ConfirmationCallbackProtocol",
    "LocalToolMetadata",
    "LocalToolsProvider",
    "MCPServerConfig",
    "MCPToolsProvider",
    "OnDemandCatalogEntry",
    "OnDemandToolCatalog",
    "OnDemandToolsView",
    "PolicyEnforcingToolsProvider",
    "PolicyEngine",
    "PolicyEvaluation",
    "PolicyRule",
    "ResolvedPolicyRule",
    "TaintTrackingToolsProvider",
    "ToolConfirmationFailed",
    "ToolConfirmationRequired",
    "ToolDescriptor",
    # From types
    "ToolExecutionContext",
    "ToolMatcher",
    "ToolNotFoundError",
    "ToolPolicyConfig",
    "ToolPolicyDecision",
    "ToolPolicyDeniedError",
    "ToolRegistration",
    "ToolTag",
    # From infrastructure
    "ToolsProvider",
    "_format_event_details_for_confirmation",
    # Helper functions
    "_scan_user_docs",
    "add_or_update_note_tool",
    "annotate_image_tool",
    "attach_to_response_tool",
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
    "build_local_tool_registrations",
    "call_home_assistant_action_tool",
    "cancel_pending_callback_tool",
    "cancel_worker_task_tool",
    "collect_system_prompt_addition",
    "computer_use_click",
    "computer_use_double_click",
    "computer_use_drag_and_drop",
    "computer_use_go_back",
    "computer_use_go_forward",
    "computer_use_hotkey",
    "computer_use_key_down",
    "computer_use_key_up",
    "computer_use_middle_click",
    "computer_use_mouse_down",
    "computer_use_mouse_up",
    "computer_use_move",
    "computer_use_navigate",
    "computer_use_press_key",
    "computer_use_right_click",
    "computer_use_scroll",
    "computer_use_take_screenshot",
    "computer_use_triple_click",
    "computer_use_type",
    "computer_use_wait",
    "create_github_issue",
    "create_vega_chart_tool",
    "delegate_to_service_tool",
    "delete_note_tool",
    "delete_script_tool",
    "download_media_tool",
    "download_state_history_tool",
    "drive_get_file_tool",
    "drive_search_tool",
    "drive_write_file_tool",
    "execute_script_tool",
    "find_provider_by_type",
    "generate_image_tool",
    "generate_video_tool",
    "get_attachment_info_tool",
    "get_camera_frame_tool",
    "get_camera_frames_batch_tool",
    "get_camera_recordings_tool",
    "get_camera_snapshot_tool",
    "get_delegation_status_tool",
    "get_full_document_content_tool",
    "get_live_camera_snapshot_tool",
    "get_llm_request_history",
    "get_mcp_server_status",
    "get_message_history_tool",
    "get_note_tool",
    "get_profile_config",
    "get_profile_tool_inventory",
    "get_resolved_config",
    "get_script_tool",
    "get_system_info",
    "get_tool_definitions_for_advertisement",
    "get_user_documentation_content_tool",
    "gmail_create_draft_tool",
    "gmail_get_attachment_tool",
    "gmail_get_message_tool",
    "gmail_search_tool",
    "highlight_image_tool",
    "ingest_document_from_url_tool",
    "jq_query_tool",
    "list_cameras_tool",
    "list_delegations_tool",
    "list_home_assistant_actions_tool",
    "list_home_assistant_entities_tool",
    "list_notes_tool",
    "list_pending_callbacks_tool",
    "list_scripts_tool",
    "list_worker_tasks_tool",
    "mock_camera_snapshot_tool",
    "modify_pending_callback_tool",
    "mqtt_publish_tool",
    "query_database",
    "query_recent_events_tool",
    "read_error_logs",
    "read_frontend_telemetry",
    "read_source_file",
    "read_task_result_tool",
    "reconnect_mcp_server",
    "reindex_email_tool",
    "render_delete_calendar_event_confirmation",
    "render_home_assistant_template_tool",
    "render_modify_calendar_event_confirmation",
    "report_technical_problem_tool",
    "resolve_tool_policy",
    "save_script_tool",
    "scan_camera_frames_tool",
    "schedule_action_tool",
    # Individual tool functions (for testing/direct use)
    "schedule_future_callback_tool",
    "schedule_reminder_tool",
    "search_camera_events_tool",
    "search_documents_tool",
    "search_source_code",
    "send_message_to_user_tool",
    "spawn_worker_tool",
    "storage",
    "test_event_listener_tool",
    "test_script_with_simulated_tools_tool",
    "transform_image_tool",
    "ucp_add_to_cart_tool",
    "ucp_get_cart_tool",
    "ucp_transfer_checkout_to_human_tool",
    "workspace_delete_tool",
    "workspace_export_notes_tool",
    "workspace_glob_tool",
    "workspace_import_note_tool",
    "workspace_mkdir_tool",
    "workspace_read_tool",
    "workspace_write_tool",
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


def _metadata(
    *tags: ToolTag,
    summary: str | None = None,
    destination_argument_paths: tuple[str, ...] = (),
    deferred_confirmation_eligible: bool = False,
) -> LocalToolMetadata:
    """Create validated metadata for a local tool registration."""
    return make_local_tool_metadata(
        list(tags),
        summary=summary,
        destination_argument_paths=destination_argument_paths,
        deferred_confirmation_eligible=deferred_confirmation_eligible,
    )


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
    + GOOGLE_DATA_TOOLS_DEFINITION
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
    # Computer Use tools (Gemini 3.5 native action space)
    "click": computer_use_click,
    "double_click": computer_use_double_click,
    "triple_click": computer_use_triple_click,
    "middle_click": computer_use_middle_click,
    "right_click": computer_use_right_click,
    "mouse_down": computer_use_mouse_down,
    "mouse_up": computer_use_mouse_up,
    "move": computer_use_move,
    "type": computer_use_type,
    "press_key": computer_use_press_key,
    "key_down": computer_use_key_down,
    "key_up": computer_use_key_up,
    "hotkey": computer_use_hotkey,
    "scroll": computer_use_scroll,
    "drag_and_drop": computer_use_drag_and_drop,
    "navigate": computer_use_navigate,
    "go_back": computer_use_go_back,
    "go_forward": computer_use_go_forward,
    "take_screenshot": computer_use_take_screenshot,
    "wait": computer_use_wait,
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
    "read_frontend_telemetry": read_frontend_telemetry,
    "create_github_issue": create_github_issue,
    "get_llm_request_history": get_llm_request_history,
    "get_mcp_server_status": get_mcp_server_status,
    "reconnect_mcp_server": reconnect_mcp_server,
    "get_resolved_config": get_resolved_config,
    "get_profile_config": get_profile_config,
    "get_profile_tool_inventory": get_profile_tool_inventory,
    "get_system_info": get_system_info,
    "resolve_tool_policy": resolve_tool_policy,
    # MQTT tools
    "mqtt_publish": mqtt_publish_tool,
    # Shopping/UCP tools
    "ucp_add_to_cart": ucp_add_to_cart_tool,
    "ucp_get_cart": ucp_get_cart_tool,
    "ucp_transfer_checkout_to_human": ucp_transfer_checkout_to_human_tool,
    # Problem reporting
    "report_technical_problem": report_technical_problem_tool,
    # Google personal data (Gmail/Drive) tools
    "gmail_search": gmail_search_tool,
    "gmail_create_draft": gmail_create_draft_tool,
    "gmail_get_message": gmail_get_message_tool,
    "gmail_get_attachment": gmail_get_attachment_tool,
    "drive_search": drive_search_tool,
    "drive_get_file": drive_get_file_tool,
    "drive_write_file": drive_write_file_tool,
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
        # OUTPUT_UNTRUSTED: a completed run's result_text is included verbatim, and
        # for a remote A2A run that text is the remote agent's own output.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "list_delegations": _metadata(
        ToolTag.DELEGATION,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        # OUTPUT_UNTRUSTED: includes completed runs' result_text verbatim, remote
        # A2A output among it.
        ToolTag.OUTPUT_UNTRUSTED,
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
        ToolTag.SENSITIVE_DATA,
        ToolTag.SCHEDULING,
        # OUTPUT_UNTRUSTED: returns stored callback text verbatim.
        ToolTag.OUTPUT_UNTRUSTED,
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
        destination_argument_paths=("url_to_ingest",),
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
        ToolTag.SENSITIVE_DATA,
        ToolTag.AUTOMATION,
        # OUTPUT_UNTRUSTED: automation definitions are returned verbatim without the
        # per-record provenance resolution that firing-time definition taint
        # computes, so a definition shaped by untrusted content reads as trusted.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_automation": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.AUTOMATION,
        # OUTPUT_UNTRUSTED: returns an automation's inline script and config
        # verbatim, with no per-record provenance resolution on read.
        ToolTag.OUTPUT_UNTRUSTED,
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
        ToolTag.SENSITIVE_DATA,
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
    # The target must be an existing conversation owned by an authorized user,
    # rejected server-side otherwise, so the model cannot name a destination of
    # its own choosing. EXTERNAL_COMM is kept so tool policies matching it are
    # unaffected; KNOWN_USER_COMM refines the taint sink class.
    "send_message_to_user": _metadata(
        ToolTag.EXTERNAL_COMM,
        ToolTag.KNOWN_USER_COMM,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
        # The send is complete without consuming its result, so an unattended
        # reviewer confirmation may safely execute it later as a durable outbox
        # item. Other tools fail closed unless they opt in just as explicitly.
        deferred_confirmation_eligible=True,
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
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_script": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
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
        ToolTag.USER_FACING_MEDIA,
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
    # The image backends take a prompt and a style; the endpoint is fixed at
    # construction and no argument selects a recipient. A fixed recipient is
    # what LOW_BANDWIDTH_EXTERNAL denotes, whereas EXTERNAL_COMM means the
    # model controls the destination -- which these tools cannot do.
    "generate_image": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.LOW_BANDWIDTH_EXTERNAL,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "transform_image": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.LOW_BANDWIDTH_EXTERNAL,
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
        ToolTag.SENSITIVE_DATA,
        ToolTag.DATA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    # Fixed vendor endpoint with no recipient argument, as for generate_image.
    "generate_video": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.LOW_BANDWIDTH_EXTERNAL,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    # Unlike the generation tools above, this one takes a URL: the model picks
    # the destination, so it is EXTERNAL_COMM and must stay that way.
    "download_media": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.EXTERNAL_COMM,
        ToolTag.MEDIA,
        ToolTag.OUTPUT_UNTRUSTED,
        destination_argument_paths=("url",),
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
    # Computer Use tools (Gemini 3.5 native action space)
    "click": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "double_click": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "triple_click": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "middle_click": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "right_click": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "mouse_down": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "mouse_up": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "move": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "type": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "press_key": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "key_down": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "key_up": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "hotkey": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "scroll": _metadata(
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
    "navigate": _metadata(
        ToolTag.BROWSER,
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
        destination_argument_paths=("url",),
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
    "take_screenshot": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "wait": _metadata(
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
        destination_argument_paths=("url",),
    ),
    "browser_snapshot": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
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
        ToolTag.SENSITIVE_DATA,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_extract": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.EXTERNAL_COMM,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "browser_screenshot": _metadata(
        ToolTag.BROWSER,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
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
        # OUTPUT_UNTRUSTED: returns stored task descriptions and results, which are
        # authored by whoever's content shaped the task.
        ToolTag.OUTPUT_UNTRUSTED,
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
        # OUTPUT_UNTRUSTED: SELECTs return message, note and intake rows verbatim,
        # including ingested email. The tool is a trusted read; its content is
        # whatever anyone ever sent the assistant.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "read_error_logs": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.FILE_SYSTEM,
        ToolTag.DOCUMENTS,
        # OUTPUT_UNTRUSTED: log lines embed whatever was logged -- request bodies,
        # exception messages built from external input -- verbatim.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "get_llm_request_history": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DATA,
        # OUTPUT_UNTRUSTED: replays prior LLM requests verbatim, so it returns the
        # browser snapshots and email bodies those turns read.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "read_frontend_telemetry": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.DATA,
        # OUTPUT_UNTRUSTED (unlike read_error_logs): the telemetry lane is fed
        # ENTIRELY by the intentionally-unauthenticated POST /api/errors/, so a
        # remote caller controls every field verbatim. Marking it untrusted makes
        # the taint resolver treat these results as an external source, so a
        # breadcrumb carrying prompt-injection text cannot reach the engineer as
        # trusted content.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "create_github_issue": _metadata(
        ToolTag.EXTERNAL_COMM,
        ToolTag.STATE_CHANGING,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "get_mcp_server_status": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        # OUTPUT_UNTRUSTED: the per-server payload lists tool names published by the
        # remote server, which the deployment does not author.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "reconnect_mcp_server": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.SENSITIVE_DATA,
        # OUTPUT_UNTRUSTED: the result embeds the same remote-authored server status
        # payload as get_mcp_server_status.
        ToolTag.OUTPUT_UNTRUSTED,
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
        # OUTPUT_UNTRUSTED: the on-demand catalog breakdown renders MCP tool names
        # and summaries, which remote servers author.
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "resolve_tool_policy": _metadata(
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
        # Publishes only to the operator-configured home broker, so runtime
        # taint treats it as a home_local sink rather than arbitrary external
        # messaging (which the external_comm tag would imply).
        ToolTag.STATE_CHANGING,
        ToolTag.HOME_AUTOMATION,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "ucp_add_to_cart": _metadata(
        ToolTag.STATE_CHANGING,
        ToolTag.EXTERNAL_COMM,
        ToolTag.SHOPPING,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "ucp_get_cart": _metadata(
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
        ToolTag.EXTERNAL_COMM,
        ToolTag.SHOPPING,
        ToolTag.OUTPUT_UNTRUSTED,
    ),
    "ucp_transfer_checkout_to_human": _metadata(
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
    # Google personal data (Gmail/Drive) tools. Content is attacker-addressable
    # (anyone can email you / share a file), so it enters the turn untrusted; the
    # read-only + sensitive tags make a second read after untrusted content a
    # gated sensitive_read_broadening sink.
    "gmail_search": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.OUTPUT_UNTRUSTED,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
    ),
    "gmail_get_message": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.OUTPUT_UNTRUSTED,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
    ),
    "gmail_get_attachment": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.OUTPUT_UNTRUSTED,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
    ),
    "drive_search": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.OUTPUT_UNTRUSTED,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
    ),
    "drive_get_file": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.OUTPUT_UNTRUSTED,
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
    ),
    # These deterministic writes persist artifacts in the acting user's own
    # connected account. A draft is not delivered, and Drive writes cannot
    # choose an external recipient or a folder outside the app boundary, so
    # both intentionally resolve to the same artifact_write sink as notes.
    "gmail_create_draft": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
        ToolTag.OUTPUT_TRUSTED,
    ),
    "drive_write_file": _metadata(
        ToolTag.CONNECTED_ACCOUNT_DATA,
        ToolTag.STATE_CHANGING,
        ToolTag.STATE_PERSISTING,
        ToolTag.SENSITIVE_DATA,
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
