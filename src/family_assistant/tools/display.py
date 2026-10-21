"""How a tool call reads to the person watching the chat.

Every client (the web thread, the shared-conversation page, iOS) shows a tool
call as a short phrase — "Searching notes for “dentist”" while it runs,
"Searched notes for “dentist”" once it has — rather than the tool's name. The
phrase is built here, once, and attached to every tool call sent to a client,
so the clients carry no phrasing of their own and cannot drift apart.

A tool without a template reads as its name in sentence case followed by its
most telling argument, which is what clients showed before phrases existed.

Arguments are model-written text. The phrase is plain text, single-line and
truncated; clients must render it as text, never as markup.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypedDict

_VALUE_MAX = 60

# Arguments that, when a tool has no template, describe the call better than
# anything else in it. Checked in order; the first short scalar wins.
_PRIMARY_ARG_KEYS = (
    "query",
    "sql",
    "title",
    "name",
    "url",
    "path",
    "prompt",
    "message",
    "text",
    "description",
    "target_service_id",
    "entity_id",
)

type _Detail = Callable[[Mapping[str, object]], str | None]


class ToolCallDisplay(TypedDict):
    """The client-facing description of one tool call."""

    active: str
    done: str
    # The call changed something (created, sent, deleted, scheduled) rather
    # than only reading. Clients summarise a finished group by such a call.
    changes: bool


@dataclass(frozen=True, slots=True)
class _Phrase:
    active: str
    done: str
    detail: _Detail | None = None
    changes: bool = False


def _single_line(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) > _VALUE_MAX:
        return collapsed[: _VALUE_MAX - 1] + "…"
    return collapsed


def _scalar(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str | int | float):
        text = _single_line(str(value))
        return text or None
    return None


def _quoted(connector: str, *keys: str) -> _Detail:
    """`connector “value”` from the first of ``keys`` holding a scalar."""

    def detail(args: Mapping[str, object]) -> str | None:
        for key in keys:
            value = _scalar(args.get(key))
            if value:
                return f"{connector} “{value}”".strip()
        return None

    return detail


def _plain(connector: str, *keys: str) -> _Detail:
    """`connector value`, for values that read badly in quotes (URLs, dates)."""

    def detail(args: Mapping[str, object]) -> str | None:
        for key in keys:
            value = _scalar(args.get(key))
            if value:
                return f"{connector} {value}".strip()
        return None

    return detail


def _first(*details: _Detail) -> _Detail:
    """The first of ``details`` that has something to say."""

    def detail(args: Mapping[str, object]) -> str | None:
        for candidate in details:
            text = candidate(args)
            if text:
                return text
        return None

    return detail


def _home_assistant_action(args: Mapping[str, object]) -> str | None:
    domain = _scalar(args.get("domain"))
    action = _scalar(args.get("action"))
    if domain and action:
        return f"{domain}.{action}"
    return None


def _calendar_range(args: Mapping[str, object]) -> str | None:
    text = _scalar(args.get("search_text"))
    if text:
        return f"for “{text}”"
    start = _scalar(args.get("start_date"))
    end = _scalar(args.get("end_date"))
    if start and end and start != end:
        return f"from {start} to {end}"
    if start:
        return f"for {start}"
    return None


_PHRASES: dict[str, _Phrase] = {
    # Notes and memory
    "get_note": _Phrase("Reading note", "Read note", _quoted("", "title")),
    "list_notes": _Phrase("Listing notes", "Listed notes"),
    "add_or_update_note": _Phrase(
        "Saving note", "Saved note", _quoted("", "title"), changes=True
    ),
    "delete_note": _Phrase(
        "Deleting note", "Deleted note", _quoted("", "title"), changes=True
    ),
    "propose_memory_edits": _Phrase(
        "Proposing memory edits", "Proposed memory edits", changes=True
    ),
    # Documents and history
    "search_documents": _Phrase(
        "Searching documents", "Searched documents", _quoted("for", "query")
    ),
    "get_full_document_content": _Phrase("Reading document", "Read document"),
    "ingest_document_from_url": _Phrase(
        "Saving document",
        "Saved document",
        _first(_quoted("", "title"), _plain("from", "url_to_ingest")),
        changes=True,
    ),
    "get_user_documentation_content": _Phrase(
        "Reading help", "Read help", _plain("on", "filename")
    ),
    "get_message_history": _Phrase(
        "Searching past messages", "Searched past messages", _quoted("for", "query")
    ),
    "read_text_attachment": _Phrase("Reading attachment", "Read attachment"),
    "get_attachment_info": _Phrase("Inspecting attachment", "Inspected attachment"),
    "attach_to_response": _Phrase("Attaching files", "Attached files"),
    "jq_query": _Phrase("Querying data", "Queried data"),
    # Calendar
    "list_calendars": _Phrase("Listing calendars", "Listed calendars"),
    "search_calendar_events": _Phrase(
        "Checking calendar", "Checked calendar", _calendar_range
    ),
    "add_calendar_event": _Phrase(
        "Adding event", "Added event", _quoted("", "summary"), changes=True
    ),
    "modify_calendar_event": _Phrase(
        "Updating event", "Updated event", _quoted("", "new_summary"), changes=True
    ),
    "delete_calendar_event": _Phrase("Deleting event", "Deleted event", changes=True),
    # Reminders and scheduling
    "schedule_reminder": _Phrase(
        "Setting reminder", "Set reminder", _quoted("", "message"), changes=True
    ),
    "schedule_future_callback": _Phrase(
        "Scheduling follow-up",
        "Scheduled follow-up",
        _plain("for", "callback_time"),
        changes=True,
    ),
    "schedule_action": _Phrase(
        "Scheduling action",
        "Scheduled action",
        _plain("for", "schedule_time"),
        changes=True,
    ),
    "list_pending_callbacks": _Phrase(
        "Checking scheduled follow-ups", "Checked scheduled follow-ups"
    ),
    "modify_pending_callback": _Phrase(
        "Rescheduling follow-up", "Rescheduled follow-up", changes=True
    ),
    "cancel_pending_callback": _Phrase(
        "Cancelling follow-up", "Cancelled follow-up", changes=True
    ),
    # Automations
    "list_automations": _Phrase("Listing automations", "Listed automations"),
    "get_automation": _Phrase("Reading automation", "Read automation"),
    "get_automation_stats": _Phrase(
        "Checking automation history", "Checked automation history"
    ),
    "create_automation": _Phrase(
        "Creating automation", "Created automation", _quoted("", "name"), changes=True
    ),
    "update_automation": _Phrase(
        "Updating automation", "Updated automation", changes=True
    ),
    "enable_automation": _Phrase(
        "Enabling automation", "Enabled automation", changes=True
    ),
    "disable_automation": _Phrase(
        "Disabling automation", "Disabled automation", changes=True
    ),
    "delete_automation": _Phrase(
        "Deleting automation", "Deleted automation", changes=True
    ),
    "query_recent_events": _Phrase("Checking recent events", "Checked recent events"),
    "test_event_listener": _Phrase("Testing event match", "Tested event match"),
    # Messaging and delegation
    "send_message_to_user": _Phrase("Sending message", "Sent message", changes=True),
    "send_to_my_chat": _Phrase(
        "Sending to your chat", "Sent to your chat", _quoted("", "title"), changes=True
    ),
    "delegate_to_service": _Phrase("Asking", "Asked", _plain("", "target_service_id")),
    "get_delegation_status": _Phrase(
        "Checking on delegated task", "Checked on delegated task"
    ),
    "list_delegations": _Phrase("Listing delegated tasks", "Listed delegated tasks"),
    "cancel_delegation": _Phrase(
        "Cancelling delegated task", "Cancelled delegated task", changes=True
    ),
    "gmail_search": _Phrase(
        "Searching email", "Searched email", _quoted("for", "query")
    ),
    "gmail_get_message": _Phrase("Reading email", "Read email"),
    "gmail_get_attachment": _Phrase(
        "Fetching email attachment", "Fetched email attachment", _quoted("", "filename")
    ),
    "gmail_create_draft": _Phrase(
        "Drafting email", "Drafted email", _quoted("", "subject"), changes=True
    ),
    "drive_search": _Phrase(
        "Searching Drive", "Searched Drive", _quoted("for", "query")
    ),
    "drive_get_file": _Phrase("Reading Drive file", "Read Drive file"),
    "drive_write_file": _Phrase(
        "Saving to Drive", "Saved to Drive", _quoted("", "name"), changes=True
    ),
    # Home and cameras
    "get_home_status": _Phrase("Checking the house", "Checked the house"),
    "get_weather_forecast": _Phrase("Checking the weather", "Checked the weather"),
    "list_home_assistant_entities": _Phrase(
        "Looking up devices",
        "Looked up devices",
        _quoted("matching", "entity_id_filter", "area_filter"),
    ),
    "list_home_assistant_actions": _Phrase(
        "Looking up device actions", "Looked up device actions"
    ),
    "call_home_assistant_action": _Phrase(
        "Running", "Ran", _home_assistant_action, changes=True
    ),
    "render_home_assistant_template": _Phrase("Reading home state", "Read home state"),
    "download_state_history": _Phrase(
        "Fetching device history", "Fetched device history"
    ),
    "get_camera_snapshot": _Phrase(
        "Taking camera snapshot",
        "Took camera snapshot",
        _plain("of", "camera_entity_id"),
    ),
    "list_cameras": _Phrase("Listing cameras", "Listed cameras"),
    "search_camera_events": _Phrase(
        "Searching camera events", "Searched camera events", _plain("on", "camera_id")
    ),
    "get_camera_frame": _Phrase(
        "Fetching camera frame", "Fetched camera frame", _plain("at", "timestamp")
    ),
    "get_camera_frames_batch": _Phrase(
        "Fetching camera frames", "Fetched camera frames", _plain("from", "camera_id")
    ),
    "get_camera_recordings": _Phrase(
        "Fetching recordings", "Fetched recordings", _plain("from", "camera_id")
    ),
    "get_live_camera_snapshot": _Phrase(
        "Taking camera snapshot", "Took camera snapshot", _plain("of", "camera_id")
    ),
    "scan_camera_frames": _Phrase(
        "Scanning camera footage", "Scanned camera footage", _quoted("for", "condition")
    ),
    "find_camera_change": _Phrase(
        "Finding when it changed",
        "Found when it changed",
        _quoted("for", "condition"),
    ),
    # Media
    "generate_image": _Phrase(
        "Generating image", "Generated image", _quoted("of", "prompt"), changes=True
    ),
    "transform_image": _Phrase(
        "Editing image", "Edited image", _quoted("to", "instruction"), changes=True
    ),
    "highlight_image": _Phrase("Marking up image", "Marked up image", changes=True),
    "annotate_image": _Phrase("Annotating image", "Annotated image", changes=True),
    "create_vega_chart": _Phrase(
        "Drawing chart", "Drew chart", _quoted("", "title"), changes=True
    ),
    "generate_video": _Phrase(
        "Generating video", "Generated video", _quoted("of", "prompt"), changes=True
    ),
    "download_media": _Phrase(
        "Downloading media", "Downloaded media", _plain("from", "url"), changes=True
    ),
    # Scripts and workers
    "execute_script": _Phrase("Running script", "Ran script", _quoted("", "name")),
    "save_script": _Phrase(
        "Saving script", "Saved script", _quoted("", "name"), changes=True
    ),
    "list_scripts": _Phrase("Listing scripts", "Listed scripts"),
    "get_script": _Phrase("Reading script", "Read script", _quoted("", "name")),
    "delete_script": _Phrase(
        "Deleting script", "Deleted script", _quoted("", "name"), changes=True
    ),
    "spawn_worker": _Phrase(
        "Starting coding agent", "Started coding agent", changes=True
    ),
    "read_task_result": _Phrase("Reading worker result", "Read worker result"),
    "list_worker_tasks": _Phrase("Listing worker tasks", "Listed worker tasks"),
    "cancel_worker_task": _Phrase(
        "Cancelling worker task", "Cancelled worker task", changes=True
    ),
    # Browsing
    "browser_open": _Phrase("Opening", "Opened", _plain("", "url")),
    "navigate": _Phrase("Opening", "Opened", _plain("", "url")),
    "browser_snapshot": _Phrase("Reading the page", "Read the page"),
    "take_screenshot": _Phrase("Looking at the page", "Looked at the page"),
    "browser_screenshot": _Phrase("Looking at the page", "Looked at the page"),
    "run_authenticated_site_task": _Phrase(
        "Working on", "Worked on", _plain("", "site_id")
    ),
    # Diagnostics
    "query_trino": _Phrase("Querying data warehouse", "Queried data warehouse"),
    "query_database": _Phrase("Querying database", "Queried database"),
    "read_source_file": _Phrase("Reading", "Read", _plain("", "file_path")),
    "search_source_code": _Phrase(
        "Searching code", "Searched code", _quoted("for", "pattern")
    ),
    "read_error_logs": _Phrase("Reading error logs", "Read error logs"),
    "create_github_issue": _Phrase(
        "Filing issue", "Filed issue", _quoted("", "title"), changes=True
    ),
    "report_technical_problem": _Phrase(
        "Reporting a problem", "Reported a problem", changes=True
    ),
}


def _parse_arguments(arguments: str | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(arguments, Mapping):
        return arguments
    try:
        parsed = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _humanize(name: str) -> str:
    words = " ".join(name.replace("-", "_").split("_")).strip().lower()
    return words[:1].upper() + words[1:] if words else "Tool"


def _fallback_detail(args: Mapping[str, object]) -> str | None:
    for key in _PRIMARY_ARG_KEYS:
        value = _scalar(args.get(key))
        if value:
            return f"· {value}"
    return None


def _join(base: str, detail: str | None) -> str:
    return f"{base} {detail}" if detail else base


def describe_tool_call(
    name: str, arguments: str | Mapping[str, object]
) -> ToolCallDisplay:
    """The phrases a client shows for a call to ``name`` with ``arguments``."""
    args = _parse_arguments(arguments)
    phrase = _PHRASES.get(name)
    if phrase is None:
        base = _humanize(name)
        detail = _fallback_detail(args)
        return {
            "active": _join(base, detail),
            "done": _join(base, detail),
            "changes": False,
        }
    detail = phrase.detail(args) if phrase.detail else None
    return {
        "active": _join(phrase.active, detail),
        "done": _join(phrase.done, detail),
        "changes": phrase.changes,
    }


def has_phrase(name: str) -> bool:
    """Whether ``name`` has a written phrase rather than the generic fallback."""
    return name in _PHRASES
