"""Tool confirmation renderers.

This module contains functions for rendering confirmation prompts
for tools that require user confirmation before execution.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol, cast

from family_assistant import calendar_integration
from family_assistant.calendar_integration import CalendarSource
from family_assistant.google_calendar import (
    GoogleCalendarClient,
    google_calendar_id_from_source_id,
    google_event_to_calendar_event,
    is_google_source_id,
)
from family_assistant.services.api_backend import ApiBackendError
from family_assistant.services.google_api import GoogleApiError
from family_assistant.services.oauth_credentials import OAuthCredentialError
from family_assistant.tools.calendar import resolve_target_caldav_url
from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES

if TYPE_CHECKING:
    from collections.abc import Mapping
    from zoneinfo import ZoneInfo

    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.types import (
        CalendarConfig,
        CalendarEvent,
        ToolArgumentsView,
        ToolExecutionContext,
    )

logger = logging.getLogger(__name__)


def _markdown_code_block(text: str) -> str:
    """Render text as inert markdown using a fence longer than any content fence."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def _confirmation_value(value: object) -> str:
    """Render a value for a confirmation prompt.

    Never truncates: an approver must see the whole payload they are approving,
    and whether it can be displayed is the delivering interface's call, not a
    renderer's (see docs/design/confirmation-prompt-capacity.md).
    """
    return "" if value is None else str(value)


def _confirmation_field(label: str, value: object) -> str:
    """Format a single confirmation field."""
    return f"- {label}:\n{_markdown_code_block(_confirmation_value(value))}"


def _extract_calendar_config_from_provider(
    provider: ToolsProvider | None,
) -> CalendarConfig | None:
    """Find the calendar config of the first LocalToolsProvider in a provider chain.

    Production chains nest several wrappers (taint tracking around policy
    enforcement around a composite), so the whole chain is walked rather than
    a fixed number of levels.
    """
    # Import here to avoid circular dependency at module load time
    from family_assistant.tools.infrastructure import (  # noqa: PLC0415
        LocalToolsProvider,
        ToolProviderComposite,
        ToolProviderWrapper,
    )

    if provider is None:
        return None
    if isinstance(provider, LocalToolsProvider):
        config = provider.get_calendar_config()
        return cast("CalendarConfig", config) if config else None
    if isinstance(provider, ToolProviderWrapper):
        return _extract_calendar_config_from_provider(provider.wrapped_provider)
    if isinstance(provider, ToolProviderComposite):
        for sub_provider in provider.get_providers():
            config = _extract_calendar_config_from_provider(sub_provider)
            if config is not None:
                return config
    return None


class ConfirmationRenderer(Protocol):
    """Protocol for confirmation prompt renderers.

    Confirmation renderers are responsible for fetching any necessary data
    and formatting a human-readable confirmation prompt. They receive the
    full ToolExecutionContext to access configuration, timezone, etc.
    """

    async def __call__(
        self,
        args: ToolArgumentsView,
        context: ToolExecutionContext,
    ) -> str:
        """Render a confirmation prompt from tool arguments.

        Args:
            args: Tool arguments (e.g., uid, calendar_url for calendar tools)
            context: Execution context with timezone, calendar config, etc.

        Returns:
            Formatted confirmation prompt string
        """
        ...


def append_review_reason_to_confirmation(
    prompt: str,
    context: ToolExecutionContext,
) -> str:
    """Append the automatic judge's reason to a human confirmation prompt."""
    reason = context.tool_call_review_confirmation_reason
    if not reason:
        return prompt
    quoted_reason = "\n".join(f"> {line}" for line in reason.splitlines())
    return f"{prompt}\n\nAutomatic review reason:\n{quoted_reason}"


def _format_event_details_for_confirmation(
    details: CalendarEvent | None,
    timezone: ZoneInfo,
) -> str:
    """Formats fetched event details for inclusion in confirmation prompts."""
    if not details:
        return "Event details not found."
    summary = details.get("summary", "No Title")
    start_obj = details.get("start")
    end_obj = details.get("end")

    start_str = (
        calendar_integration.format_datetime_or_date(start_obj, timezone, is_end=False)
        if start_obj
        else "Unknown Start Time"
    )
    end_str = (
        calendar_integration.format_datetime_or_date(end_obj, timezone, is_end=True)
        if end_obj
        else "Unknown End Time"
    )
    all_day = details.get("all_day", False)
    if all_day:
        return f"'{summary}' (All Day: {start_str})"
    else:
        return f"'{summary}' ({start_str} - {end_str})"


async def _fetch_event_details_for_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
    *,
    operation_verb: str,
) -> CalendarEvent | None:
    """Look up the event a modify/delete call targets, or None if not found."""
    raw_uid = args.get("uid")
    raw_calendar_url = args.get("calendar_url")
    raw_calendar_id = args.get("calendar_id")
    uid = raw_uid if isinstance(raw_uid, str) else None
    calendar_url = raw_calendar_url if isinstance(raw_calendar_url, str) else None
    calendar_id = raw_calendar_id if isinstance(raw_calendar_id, str) else None
    if uid is None:
        return None

    if calendar_id and is_google_source_id(calendar_id):
        return await _fetch_google_event_for_confirmation(context, calendar_id, uid)

    calendar_config = _extract_calendar_config_from_provider(
        getattr(context, "tools_provider", None)
    )
    if (calendar_url or calendar_id) and calendar_config:
        resolved_url, err = resolve_target_caldav_url(
            calendar_config=calendar_config,
            calendar_url=calendar_url,
            calendar_id=calendar_id,
            operation_verb=operation_verb,
        )
        calendar_url = resolved_url if not err else None

    if not calendar_url or not calendar_config:
        return None
    return await calendar_integration.fetch_event_details_for_confirmation(
        uid=uid,
        calendar_url=calendar_url,
        calendar_config=calendar_config,
        timezone=context.timezone,
    )


async def _fetch_google_event_for_confirmation(
    context: ToolExecutionContext, source_id: str, uid: str
) -> CalendarEvent | None:
    client = GoogleCalendarClient.from_exec_context(context)
    calendar_id = google_calendar_id_from_source_id(source_id)
    if client is None or calendar_id is None:
        return None
    try:
        item = await client.get_event(calendar_id, uid)
    except (OAuthCredentialError, GoogleApiError, ApiBackendError) as exc:
        logger.info("Could not fetch Google event for confirmation: %s", exc)
        return None
    source = CalendarSource(
        source_id=source_id,
        name=source_id,
        kind="google",
        url="",
        writable=True,
        google_calendar_id=calendar_id,
    )
    return google_event_to_calendar_event(item, source, context.timezone)


async def render_delete_calendar_event_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Renders the confirmation message for deleting a calendar event.

    Fetches event details from the calendar to provide a meaningful prompt.

    Args:
        args: Tool arguments including uid and calendar_url
        context: Execution context with calendar config and timezone
    """
    # Fetch event details to show the user what they're deleting
    event_details = await _fetch_event_details_for_confirmation(
        args, context, operation_verb="delete"
    )

    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(event_details, context.timezone)

    return (
        "Please confirm you want to *delete* the event:\n"
        f"Event:\n{_markdown_code_block(event_desc)}"
    )


async def render_modify_calendar_event_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Renders the confirmation message for modifying a calendar event.

    Fetches event details from the calendar to provide a meaningful prompt.

    Args:
        args: Tool arguments including uid, calendar_url, and modification fields
        context: Execution context with calendar config and timezone
    """
    # Fetch event details to show the user what they're modifying
    event_details = await _fetch_event_details_for_confirmation(
        args, context, operation_verb="modify"
    )

    # Use the helper to format event details
    # It handles the None case by returning "Event details not found."
    event_desc = _format_event_details_for_confirmation(event_details, context.timezone)

    changes = []
    if args.get("new_summary") is not None:
        changes.append(
            "- Set summary to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_summary']))}"
        )
    if args.get("new_start_time") is not None:
        changes.append(
            "- Set start time to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_start_time']))}"
        )
    if args.get("new_end_time") is not None:
        changes.append(
            "- Set end time to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_end_time']))}"
        )
    if args.get("new_description") is not None:
        changes.append(
            "- Set description to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_description']))}"
        )
    if args.get("new_all_day") is not None:
        changes.append(
            "- Set all-day status to:\n"
            f"{_markdown_code_block(_confirmation_value(args['new_all_day']))}"
        )

    return (
        f"Please confirm you want to *modify* the event:\n"
        f"Event:\n{_markdown_code_block(event_desc)}\n"
        f"With the following changes:\n" + "\n".join(changes)
    )


async def render_add_calendar_event_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for creating a calendar event."""
    fields = [
        _confirmation_field("Title", args.get("summary")),
    ]

    calendar_config = _extract_calendar_config_from_provider(
        getattr(context, "tools_provider", None)
    )
    raw_calendar_id = args.get("calendar_id")
    raw_calendar_url = args.get("calendar_url")
    calendar_id = raw_calendar_id if isinstance(raw_calendar_id, str) else None
    calendar_url = raw_calendar_url if isinstance(raw_calendar_url, str) else None

    calendar_label: str | None = None
    if calendar_config:
        sources = calendar_integration.resolve_calendar_sources(calendar_config)
        target_source: calendar_integration.CalendarSource | None = None
        if calendar_id:
            target_source = next(
                (s for s in sources if s.source_id == calendar_id), None
            )
        elif calendar_url:
            target_source = next((s for s in sources if s.url == calendar_url), None)
        elif sources:
            target_source = next(
                (s for s in sources if s.writable and s.is_default), None
            )

        if target_source:
            calendar_label = f"{target_source.name} ({target_source.source_id})"
        elif calendar_id:
            calendar_label = calendar_id
        elif calendar_url:
            calendar_label = calendar_url
    elif calendar_id:
        calendar_label = calendar_id
    elif calendar_url:
        calendar_label = calendar_url

    if calendar_label:
        fields.append(_confirmation_field("Calendar", calendar_label))

    fields.extend([
        _confirmation_field("Start", args.get("start_time")),
        _confirmation_field("End", args.get("end_time")),
        _confirmation_field("All day", args.get("all_day", False)),
    ])
    if args.get("location"):
        fields.append(_confirmation_field("Location", args.get("location")))
    if args.get("recurrence_rule"):
        fields.append(_confirmation_field("Recurrence", args.get("recurrence_rule")))
    if args.get("description"):
        fields.append(_confirmation_field("Description", args.get("description")))
    return "Please confirm you want to *create* this calendar event:\n" + "\n".join(
        fields
    )


async def render_add_or_update_note_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for creating or updating a note.

    Shows both the requested labels and the *effective* labels after the active
    profile's write policy, so an approver never rubber-stamps a payload whose
    visibility differs from what the runtime will actually persist.
    """
    fields = [
        _confirmation_field("Title", args.get("title")),
        _confirmation_field("Append", args.get("append", False)),
        _confirmation_field("Include in prompt", args.get("include_in_prompt", False)),
        _confirmation_field("Content", args.get("content")),
    ]

    requested_raw = args.get("visibility_labels")
    requested_labels: list[str] | None = None
    if isinstance(requested_raw, list):
        requested_labels = [str(label) for label in requested_raw]
        fields.append(_confirmation_field("Requested visibility labels", requested_raw))

    fields.append(
        _confirmation_field(
            "Effective visibility labels",
            await _effective_note_labels(args, context, requested_labels),
        )
    )
    return "Please confirm you want to *save* this note:\n" + "\n".join(fields)


async def _effective_note_labels(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
    requested_labels: list[str] | None,
) -> str:
    """Compute the labels a note write would actually persist, for display."""
    # Local import: the notes repository transitively imports the tools package
    # (repositories/__init__ -> schedule_automations -> task_worker -> tools),
    # so a top-level import here would be circular.
    from family_assistant.storage.repositories.notes import (  # noqa: PLC0415
        NoteReadPolicy,
        NoteWritePolicyError,
    )

    write_policy = context.note_write_policy()
    title = args.get("title")
    existing = None
    if isinstance(title, str) and context.db_context is not None:
        existing = await context.db_context.notes.get_by_title(
            title, read_policy=NoteReadPolicy.UNRESTRICTED
        )
        # Mirror the repository's see-before-overwrite check so the approver
        # sees the rejection up front instead of approving a write that
        # add_or_update will refuse: a restricted profile may not overwrite a
        # note it cannot see.
        if existing is not None and write_policy.visibility_grants is not None:
            visible_existing = await context.db_context.notes.get_by_title(
                title, read_policy=write_policy.see_before_overwrite_read_policy()
            )
            if visible_existing is None:
                return (
                    f"REJECTED by profile policy: cannot modify note '{title}' - "
                    "insufficient visibility permissions."
                )
    try:
        effective = write_policy.resolve_labels(
            is_new_note=existing is None,
            requested_labels=requested_labels,
            existing_labels=existing.visibility_labels if existing else [],
        )
    except NoteWritePolicyError as err:
        return f"REJECTED by profile policy: {err}"
    return str(effective) if effective else "(unrestricted)"


async def render_schedule_reminder_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for scheduling a reminder."""
    _ = context
    fields = [
        _confirmation_field("Reminder time", args.get("reminder_time")),
        _confirmation_field("Message", args.get("message")),
    ]
    if args.get("follow_up"):
        fields.extend([
            _confirmation_field("Follow up", args.get("follow_up")),
            _confirmation_field("Follow-up interval", args.get("follow_up_interval")),
            _confirmation_field("Max follow-ups", args.get("max_follow_ups")),
        ])
    return "Please confirm you want to *schedule* this reminder:\n" + "\n".join(fields)


async def render_schedule_future_callback_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for scheduling a future assistant callback."""
    _ = context
    fields = [
        _confirmation_field("Callback time", args.get("callback_time")),
        _confirmation_field("Context", args.get("context")),
    ]
    return (
        "Please confirm you want to *schedule* this future assistant callback:\n"
        + "\n".join(fields)
    )


async def render_modify_pending_callback_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for modifying a pending callback."""
    _ = context
    fields = [_confirmation_field("Task ID", args.get("task_id"))]
    if args.get("new_callback_time") is not None:
        fields.append(_confirmation_field("New time", args.get("new_callback_time")))
    if args.get("new_context") is not None:
        fields.append(_confirmation_field("New context", args.get("new_context")))
    return "Please confirm you want to *modify* this callback:\n" + "\n".join(fields)


async def render_send_message_to_user_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for sending a message to a known user."""
    _ = context
    fields = [
        _confirmation_field("Target conversation", args.get("target_chat_id")),
        _confirmation_field("Message", args.get("message_content")),
    ]
    if args.get("attachment_ids"):
        fields.append(_confirmation_field("Attachments", args.get("attachment_ids")))
    return "Please confirm you want to *send* this message:\n" + "\n".join(fields)


async def render_gmail_create_draft_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render the recipients and content of an unsent Gmail draft."""
    _ = context
    fields = [
        _confirmation_field("To", args.get("to")),
        _confirmation_field("Subject", args.get("subject")),
        _confirmation_field("Body", args.get("body")),
    ]
    if args.get("cc"):
        fields.append(_confirmation_field("CC", args.get("cc")))
    if args.get("bcc"):
        fields.append(_confirmation_field("BCC", args.get("bcc")))
    if args.get("attachment_ids"):
        fields.append(_confirmation_field("Attachments", args.get("attachment_ids")))
    return "Please confirm you want to create this *unsent Gmail draft*:\n" + "\n".join(
        fields
    )


async def render_drive_write_file_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render the destination name and content source of an app-folder write."""
    _ = context
    fields = [
        _confirmation_field("Name", args.get("name") or "Use the attachment filename"),
        _confirmation_field("Overwrite existing app file", bool(args.get("overwrite"))),
    ]
    if args.get("attachment_id"):
        fields.append(_confirmation_field("Attachment", args.get("attachment_id")))
    else:
        fields.append(
            _confirmation_field("File type", args.get("file_type", "google_doc"))
        )
        fields.append(_confirmation_field("Content", args.get("content")))
    return (
        "Please confirm you want to write this file inside the app's dedicated "
        "Google Drive folder:\n" + "\n".join(fields)
    )


async def render_ingest_document_from_url_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for ingesting a document from a URL."""
    _ = context
    fields = [
        _confirmation_field("URL", args.get("url_to_ingest")),
        _confirmation_field("Source type", args.get("source_type")),
        _confirmation_field("Source ID", args.get("source_id")),
    ]
    if args.get("title"):
        fields.append(_confirmation_field("Title", args.get("title")))
    if args.get("metadata_json"):
        fields.append(_confirmation_field("Metadata", args.get("metadata_json")))
    return (
        "Please confirm you want to *fetch and index* this document"
        " (the source type and ID determine which document record is created or overwritten):\n"
        + "\n".join(fields)
    )


async def render_delegate_to_service_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for delegating a task to another service."""
    target_service_id = str(args.get("target_service_id", "")).strip()
    user_request = str(args.get("user_request", "")).strip()
    raw_attachment_ids = args.get("attachment_ids")
    attachment_ids = (
        [str(a) for a in raw_attachment_ids]
        if isinstance(raw_attachment_ids, (list, tuple))
        else []
    )

    _ = context
    fields = [
        _confirmation_field("Target profile", target_service_id or "target service"),
        f"- Request:\n{_markdown_code_block(user_request)}",
    ]
    if attachment_ids:
        fields.append(_confirmation_field("Attachments", ", ".join(attachment_ids)))
    model_tier = str(args.get("model_tier", "")).strip()
    if model_tier:
        # Named because it is a spending decision the approver is being asked
        # to make alongside the delegation itself.
        fields.append(
            _confirmation_field(
                "Intelligence",
                f"{model_tier} — the target profile runs this request on that "
                "model tier rather than its usual one.",
            )
        )
    resume_delegation_id = str(args.get("resume_delegation_id", "")).strip()
    if resume_delegation_id:
        fields.append(
            _confirmation_field(
                "Resuming delegation",
                f"{resume_delegation_id} — the target profile continues this earlier "
                "delegation's conversation and keeps its prior context, rather than "
                "starting a fresh handoff.",
            )
        )
    return "Do you want to delegate this task to another profile?\n" + "\n".join(fields)


def _spawn_worker_confirmation_prompt(arguments: Mapping[str, object]) -> str:
    """Build the full spawn_worker confirmation prompt."""
    task_description = str(arguments.get("task_description", "")).strip()

    fields = [
        _confirmation_field("Agent", arguments.get("agent", "claude")),
        f"- Task description:\n{_markdown_code_block(task_description)}",
    ]
    raw_context_paths = arguments.get("context_paths")
    if isinstance(raw_context_paths, (list, tuple)):
        if raw_context_paths:
            fields.append(
                _confirmation_field(
                    "Context paths", ", ".join(str(path) for path in raw_context_paths)
                )
            )
    elif raw_context_paths is not None:
        # Script callers bypass JSON-schema validation, so a non-list value
        # (e.g. a mapping whose keys the tool would later iterate as paths)
        # must not be silently omitted from the prompt: the guard refuses the
        # call (see confirmation_arguments_block_reason), and the prompt says so.
        fields.append(
            f"- Context paths: ⚠️ Malformed value of type "
            f"{type(raw_context_paths).__name__} — context_paths must be an array of "
            "workspace path strings. The worker will not be launched."
        )
    fields.append(
        _confirmation_field("Timeout (minutes)", arguments.get("timeout_minutes", 30))
    )
    return (
        "Do you want to launch an isolated AI coding worker? It executes code in a "
        "sandboxed container with network access — it can clone public git "
        "repositories, including this application's — but has no access to Family "
        "Assistant tools or data. It works from the task description below and "
        "returns output files:\n" + "\n".join(fields)
    )


async def render_spawn_worker_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for launching an isolated AI coding worker."""
    _ = context
    return _spawn_worker_confirmation_prompt(args)


async def render_cancel_worker_task_confirmation(
    args: ToolArgumentsView,
    context: ToolExecutionContext,
) -> str:
    """Render a confirmation prompt for cancelling a worker task.

    Looks the task up so the approver sees what they are stopping, not just an
    opaque id. Mirrors cancel_worker_task_tool's conversation scoping: a task
    belonging to a different conversation is treated as not found, so the
    prompt never leaks another conversation's task details for a cancel that
    would be refused anyway.
    """
    task_id = str(args.get("task_id", "")).strip()
    fields = [_confirmation_field("Task ID", task_id)]

    task = None
    db_context = getattr(context, "db_context", None)
    if task_id and db_context is not None:
        task = await db_context.worker_tasks.get_task(task_id)
        if task is not None and task.get("conversation_id") != context.conversation_id:
            task = None

    if task is not None:
        fields.append(_confirmation_field("Status", task.get("status")))
        fields.append(
            _confirmation_field("Task description", task.get("task_description"))
        )
    else:
        fields.append(
            "- Task details: not found — the task may have already finished or the "
            "id may be wrong."
        )
    return "Do you want to *cancel* this worker task?\n" + "\n".join(fields)


def _generic_arguments_json(arguments: Mapping[str, object]) -> str:
    """Serialize arbitrary tool arguments for a confirmation prompt."""
    return json.dumps(dict(arguments), indent=2, sort_keys=True, default=str)


def render_generic_tool_confirmation(
    tool_name: str,
    arguments: Mapping[str, object],
) -> str:
    """Render the confirmation prompt for a tool with no dedicated renderer.

    Every interface falls back to this when ``TOOL_CONFIRMATION_RENDERERS`` has
    no entry for the tool, which is every MCP tool: their names and schemas come
    from the server, so no static renderer can exist for them. Naming the tool
    alone would let an approver authorize arguments they never saw -- a shell
    command, a request body -- so the arguments themselves are the prompt.
    """
    arguments_json = _generic_arguments_json(arguments)
    return (
        "Do you want to run this tool call? It runs with the arguments below, "
        "exactly as shown:\n"
        f"{_confirmation_field('Tool', tool_name)}\n"
        f"- Arguments:\n{_markdown_code_block(arguments_json)}"
    )


def confirmation_arguments_block_reason(
    tool_name: str,
    arguments: Mapping[str, object],
) -> str | None:
    """Return why a confirm-gated call cannot be rendered faithfully, else None.

    This is not a size rule. How much of a prompt an approver can read is a
    property of the interface that renders it, and only that interface may
    refuse on those grounds -- see
    docs/design/confirmation-prompt-capacity.md. What is left here are
    arguments no interface could show correctly at any length, because the
    prompt they produce would not describe what the tool would do.
    """
    if tool_name == "spawn_worker":
        # The context paths scope what the worker can read. Script callers
        # bypass JSON-schema validation, so a present-but-non-list value is
        # refused outright: the tool would later iterate it (a mapping's keys
        # would become paths) while the prompt showed the approver no paths.
        raw_context_paths = arguments.get("context_paths")
        if raw_context_paths is not None and not isinstance(
            raw_context_paths, (list, tuple)
        ):
            return (
                f"Error: context_paths must be an array of workspace path strings, "
                f"got {type(raw_context_paths).__name__}. Pass the paths as a JSON "
                'array (e.g. ["shared/data/input.csv"]).'
            )
    return None


def make_computer_use_safety_confirmation_renderer(
    action_name: str,
) -> ConfirmationRenderer:
    """Build the safety-confirmation renderer for one computer-use action.

    The renderer protocol doesn't receive the tool name, and coordinate-only
    actions (``click``, ``right_click``, ``move``, …) are indistinguishable
    from their arguments alone — so each action gets a renderer with its name
    baked in, ensuring the user always sees exactly which action they approve.
    """

    async def render_computer_use_safety_confirmation(
        args: ToolArgumentsView,
        context: ToolExecutionContext,
    ) -> str:
        _ = context
        safety_decision = args.get("safety_decision")

        explanation = "No explanation provided"
        if isinstance(safety_decision, dict):
            explanation = str(safety_decision.get("explanation", explanation))

        fields = [
            _confirmation_field("Action", action_name),
            _confirmation_field("Explanation", explanation),
        ]

        if args.get("intent"):
            fields.append(_confirmation_field("Intent", args.get("intent")))

        for key, value in sorted(args.items()):
            if key not in {"safety_decision", "intent"}:
                fields.append(_confirmation_field(key, value))

        return (
            "Computer-use safety check: the model has flagged a potential safety "
            "concern for this browser action. Please review and approve if you "
            "want to proceed:\n" + "\n".join(fields)
        )

    return render_computer_use_safety_confirmation


# Mapping of tool names to their confirmation renderers
_base_renderers: dict[str, ConfirmationRenderer] = {
    "add_calendar_event": render_add_calendar_event_confirmation,
    "delete_calendar_event": render_delete_calendar_event_confirmation,
    "modify_calendar_event": render_modify_calendar_event_confirmation,
    "add_or_update_note": render_add_or_update_note_confirmation,
    "schedule_reminder": render_schedule_reminder_confirmation,
    "schedule_future_callback": render_schedule_future_callback_confirmation,
    "modify_pending_callback": render_modify_pending_callback_confirmation,
    "send_message_to_user": render_send_message_to_user_confirmation,
    "gmail_create_draft": render_gmail_create_draft_confirmation,
    "drive_write_file": render_drive_write_file_confirmation,
    "ingest_document_from_url": render_ingest_document_from_url_confirmation,
    "delegate_to_service": render_delegate_to_service_confirmation,
    "spawn_worker": render_spawn_worker_confirmation,
    "cancel_worker_task": render_cancel_worker_task_confirmation,
}

TOOL_CONFIRMATION_RENDERERS: dict[str, ConfirmationRenderer] = {
    **_base_renderers,
    **{
        name: make_computer_use_safety_confirmation_renderer(name)
        for name in COMPUTER_USE_FUNCTION_NAMES
    },
}
