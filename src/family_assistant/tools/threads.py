"""Conversation threads: starting them from Home, finishing them, and Home's view.

A thread is an ordinary conversation, spun off Home for work that outlives the
turn, running on whichever profile suits the work. Starting one is a delegation
whose result is delivered differently: the first turn runs on the delegation
machinery, admitted by the same policy, but it runs at the top level of the
thread's own conversation and wakes nobody when it ends -- the user talks to
the thread directly from then on.

A thread's assistant ends the job with ``finish_thread``, and Home's assistant
reads each thread's title and state -- facts, never transcripts -- with
``list_threads``. See docs/design/one-thread-chat-ux.md.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from family_assistant.llm.messages import MessageAttachmentMetadata
from family_assistant.llm.model_selection import (
    ModelSelectionRequest,
    ModelTierNotPermitted,
    resolve_model_selection,
)
from family_assistant.security.taint import (
    TurnTaintState,
    merge_taint_state_into_tracker,
)
from family_assistant.storage.tasks import TaskPriority
from family_assistant.threads.boundary import AssistantRequest
from family_assistant.threads.creation import (
    NewThread,
    create_thread,
    new_thread_conversation_id,
)
from family_assistant.threads.home import find_home, home_owner_id
from family_assistant.threads.listing import summarize_threads
from family_assistant.tools.services import (
    DELEGATED_PROFILE_RUN_TASK_TYPE,
    current_taint_metadata,
    delegated_run_review_record,
    delegation_content_parts,
    delegation_interface_type,
    delegation_tools_config,
    resolve_delegation_target,
)
from family_assistant.tools.types import ToolResult
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import (
        DatabaseExecutor,
        DatabaseTransaction,
    )
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)

TOOL_NAME = "start_thread"
FINISH_TOOL_NAME = "finish_thread"
LIST_TOOL_NAME = "list_threads"
READ_HOME_TOOL_NAME = "read_home_messages"
THREAD_TOOL_NAMES = frozenset({
    TOOL_NAME,
    FINISH_TOOL_NAME,
    LIST_TOOL_NAME,
    READ_HOME_TOOL_NAME,
})
"""Tools that only make sense in one kind of conversation, and are hidden elsewhere."""

# Home's assistant reads the most recently active threads; older ones are in
# the switcher, and a long tail of finished jobs is not what a status question
# is about.
LIST_THREADS_LIMIT = 20

# Enough to reach the reminder or brief a reply is about, and not so much that
# a thread takes on Home at large.
READ_HOME_LIMIT = 10

THREAD_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Moves a piece of work out of the user's Home conversation into a "
                "thread of its own, run by the profile best suited to it. Use it "
                "for work that outlives this turn: anything that runs in the "
                "background (browsing, research, delegated jobs), waits on "
                "someone, takes several steps the user will want to follow, or "
                "is a project they will come back to. Answer quick questions, "
                "small edits and reminders here in Home instead.\n\n"
                "The thread starts with your request and nothing else from this "
                "conversation, so write a request that stands on its own. It "
                "returns at once: tell the user you have started a thread and "
                "end your turn. The thread's progress and answers appear in the "
                "thread, not here, so do not wait for it or report back on it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_service_id": {
                        "type": "string",
                        "description": "The profile that runs the thread, from those listed in your system prompt (e.g. 'browser_profile', 'complex_tasks').",
                    },
                    "title": {
                        "type": "string",
                        "description": "A short title for the thread, a few words naming the job (e.g. 'Hot water system quotes').",
                    },
                    "request": {
                        "type": "string",
                        "description": "The complete, self-contained request the thread starts from.",
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional attachment UUIDs from this conversation to pass to the thread with the request.",
                    },
                    "model_tier": {
                        "type": "string",
                        "description": "Optional. Run the thread on a specific model tier, as listed with the target profile in your system prompt. Omit to use the profile's default.",
                    },
                },
                "required": ["target_service_id", "title", "request"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": FINISH_TOOL_NAME,
            "description": (
                "Closes this thread's job. You are working in a thread: a "
                "conversation spun off the user's Home for one piece of work. "
                "Call this once the job is complete (outcome 'done'), or once it "
                "turns out it cannot be done (outcome 'failed').\n\n"
                "After calling it, end your turn with a final message that "
                "states the outcome on its own -- what was found, decided or "
                "done, or why it could not be. That message is copied into the "
                "user's Home as the thread's result, so write it to be read "
                "there without the rest of the thread.\n\n"
                "Do not call it when your reply asks the user something or "
                "waits on them: a thread that ends on a question is waiting on "
                "the user, not done. Do not call it for progress updates. If "
                "something new arrives in the thread later, it reopens, and you "
                "can finish it again."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": ["done", "failed"],
                        "description": "'done' when the job is complete; 'failed' when it cannot be done.",
                    },
                },
                "required": ["outcome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_TOOL_NAME,
            "description": (
                "Lists the threads of the user's Home, most recently active "
                "first: each one's title, the profile that runs it, and its "
                "state -- working, waiting on the user, done or failed -- plus, "
                "for a thread its assistant finished, the outcome it reported. "
                "Use it to answer questions about work that was moved into "
                "threads ('how are the quotes going?'). It does not show what "
                "was said in a thread; if the user wants the detail, point them "
                "to the thread."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": READ_HOME_TOOL_NAME,
            "description": (
                "Reads the latest messages in the user's Home, oldest first. "
                "You are working in a thread, which starts without seeing Home, "
                "where reminders, briefs and automation results arrive. Use this "
                "when the user refers to something you can't see, such as "
                "'snooze that' or 'about that reminder'. Don't use it otherwise: "
                "most of what is in Home has nothing to do with this thread."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _error(text: str) -> ToolResult:
    return ToolResult(text=f"Error: {text}", attachments=None)


def _app_config(exec_context: ToolExecutionContext) -> AppConfig | None:
    service = exec_context.processing_service
    return getattr(service, "app_config", None) if service is not None else None


async def thread_start_refusal(
    db: DatabaseExecutor,
    *,
    app_config: AppConfig | None,
    async_delegation_enabled: bool,
    in_script: bool,
    interface_type: str,
    conversation_id: str,
    subconversation_id: str | None,
    user_id: str | None,
) -> str | None:
    """Why a turn cannot start a thread, or None if it can.

    Threads hang off Home and do not nest, so the turn must be running at the
    top level of the acting user's Home. Starting one is a background run, so
    it needs the asynchronous delegation machinery. The model loop asks this
    too, and does not advertise the tool on a turn it would refuse: a tool that
    refuses every call is a dead end advertised as a capability.
    """
    if app_config is None or not app_config.conversation_threads.enabled:
        return "Threads are not enabled."
    if in_script or not async_delegation_enabled:
        return "Threads need asynchronous delegation, which is not available here."
    if subconversation_id is not None or not user_id:
        return "A thread can only be started from the user's Home."
    home = await find_home(
        db, app_config, user_id=user_id, interface_type=interface_type
    )
    if home != conversation_id:
        return "A thread can only be started from the user's Home."
    return None


async def home_read_refusal(
    db: DatabaseExecutor,
    *,
    app_config: AppConfig | None,
    interface_type: str,
    conversation_id: str,
    subconversation_id: str | None,
    user_id: str | None,
) -> str | None:
    """Why a turn cannot read its Home's threads, or None if it can.

    Only Home's own assistant sees its threads: a thread does not see its
    siblings.
    """
    if app_config is None or not app_config.conversation_threads.enabled:
        return "Threads are not enabled."
    if subconversation_id is not None or not user_id:
        return "Threads can only be listed from the user's Home."
    home = await find_home(
        db, app_config, user_id=user_id, interface_type=interface_type
    )
    if home != conversation_id:
        return "Threads can only be listed from the user's Home."
    return None


async def thread_finish_refusal(
    db: DatabaseExecutor,
    *,
    app_config: AppConfig | None,
    conversation_id: str,
    subconversation_id: str | None,
) -> str | None:
    """Why a turn cannot finish a thread, or None if it can.

    Only the thread's own top-level turn finishes it: a delegate working inside
    a thread reports back to the thread's assistant, which decides.
    """
    if app_config is None or not app_config.conversation_threads.enabled:
        return "Threads are not enabled."
    if subconversation_id is not None:
        return "Only the thread's own assistant can finish a thread."
    if await db.conversation_threads.get_thread(conversation_id) is None:
        return "This conversation is not a thread, so there is nothing to finish."
    return None


async def read_home_refusal(
    db: DatabaseExecutor,
    *,
    app_config: AppConfig | None,
    receives_household_context: bool,
    conversation_id: str,
    subconversation_id: str | None,
) -> str | None:
    """Why a turn cannot read its thread's Home, or None if it can.

    Only a thread's own top-level turn looks back at Home, and only on a profile
    that receives household context: the same gate that keeps Home turns out of
    any other profile when they are copied.
    """
    if app_config is None or not app_config.conversation_threads.enabled:
        return "Threads are not enabled."
    if subconversation_id is not None:
        return "Only the thread's own assistant can read Home."
    if await db.conversation_threads.get_thread(conversation_id) is None:
        return "This conversation is not a thread."
    if not receives_household_context:
        return "This assistant does not see Home."
    return None


async def thread_tool_refusal(
    tool_name: str,
    db: DatabaseExecutor,
    *,
    app_config: AppConfig | None,
    async_delegation_enabled: bool,
    in_script: bool,
    receives_household_context: bool,
    interface_type: str,
    conversation_id: str,
    subconversation_id: str | None,
    user_id: str | None,
) -> str | None:
    """Why a turn cannot use one of :data:`THREAD_TOOL_NAMES`, or None if it can.

    The model loop asks this for each of them and does not advertise one a turn
    would refuse; each tool asks again when called.
    """
    if tool_name == TOOL_NAME:
        return await thread_start_refusal(
            db,
            app_config=app_config,
            async_delegation_enabled=async_delegation_enabled,
            in_script=in_script,
            interface_type=interface_type,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            user_id=user_id,
        )
    if tool_name == FINISH_TOOL_NAME:
        return await thread_finish_refusal(
            db,
            app_config=app_config,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
        )
    if tool_name == LIST_TOOL_NAME:
        return await home_read_refusal(
            db,
            app_config=app_config,
            interface_type=interface_type,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            user_id=user_id,
        )
    if tool_name == READ_HOME_TOOL_NAME:
        return await read_home_refusal(
            db,
            app_config=app_config,
            receives_household_context=receives_household_context,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
        )
    raise ValueError(f"{tool_name!r} is not a thread tool.")


async def finish_thread_tool(
    exec_context: ToolExecutionContext, outcome: str
) -> ToolResult:
    """Close the thread this turn runs in, as done or failed.

    Only the close is recorded here. The echo into Home is the message the turn
    ends with, so it is written when the turn ends (see ``threads.echo``).
    """
    refusal = await thread_finish_refusal(
        exec_context.db_context,
        app_config=_app_config(exec_context),
        conversation_id=exec_context.conversation_id,
        subconversation_id=exec_context.subconversation_id,
    )
    if refusal is not None:
        return _error(refusal)
    if outcome not in {"done", "failed"}:
        return _error("The outcome must be 'done' or 'failed'.")
    now = (exec_context.clock or SystemClock()).now()
    await exec_context.db_context.conversation_threads.close_thread(
        exec_context.conversation_id,
        outcome="done" if outcome == "done" else "failed",
        closed_by="assistant",
        turn_id=exec_context.turn_id,
        now=now,
    )
    logger.info(
        "Thread %s finished by its assistant as %s",
        exec_context.conversation_id,
        outcome,
    )
    return ToolResult(
        text=(
            f"Marked the thread {outcome}. Now end your turn with a final message "
            "stating the outcome; it is copied into the user's Home as this "
            "thread's result."
        ),
        attachments=None,
        data={"outcome": outcome},
    )


_STATE_DESCRIPTIONS = {
    "working": "working",
    "waiting": "waiting on the user",
    "done": "done",
    "failed": "failed",
}


async def list_threads_tool(exec_context: ToolExecutionContext) -> ToolResult:
    """The facts of the Home's threads: title, profile, state and echoed outcome.

    Every fact carries its taint into the turn: a title the taint of the turn
    that wrote it, an outcome that of the thread message it echoes. The state is
    derived by code. Transcripts and generated summaries are never read.
    """
    refusal = await home_read_refusal(
        exec_context.db_context,
        app_config=_app_config(exec_context),
        interface_type=exec_context.interface_type,
        conversation_id=exec_context.conversation_id,
        subconversation_id=exec_context.subconversation_id,
        user_id=exec_context.user_id,
    )
    if refusal is not None:
        return _error(refusal)
    db = exec_context.db_context
    threads = await db.conversation_threads.list_for_home(exec_context.conversation_id)
    service = exec_context.processing_service
    live_stream = (
        getattr(service, "home_activity_publisher", None)
        if service is not None
        else None
    )
    summaries = sorted(
        await summarize_threads(
            db,
            threads,
            # Only the live stream knows of a turn in progress there.
            turn_running=(
                live_stream.has_running_turn
                if live_stream is not None
                else lambda _conversation_id: False
            ),
            viewer=None,
        ),
        key=lambda summary: summary.updated_at,
        reverse=True,
    )[:LIST_THREADS_LIMIT]
    title_taints = {thread.conversation_id: thread.title_taint for thread in threads}
    facts: list[dict[str, object]] = []
    lines: list[str] = []
    for summary in summaries:
        _merge_fact_taint(exec_context, title_taints[summary.conversation_id])
        outcome = await _echoed_outcome(exec_context, summary.echo_message_id)
        fact: dict[str, object] = {
            "conversation_id": summary.conversation_id,
            "title": summary.title,
            "profile_id": summary.profile_id,
            "state": summary.state,
            "closed_by": summary.closed_by,
            "last_activity": summary.updated_at.isoformat(),
        }
        line = (
            f'- "{summary.title}" ({summary.profile_id}): '
            f"{_STATE_DESCRIPTIONS[summary.state]}"
        )
        if summary.closed_by == "user":
            line += ", closed by the user"
        if outcome is not None:
            fact["outcome"] = outcome
            line += f". Outcome it reported: {outcome}"
        facts.append(fact)
        lines.append(line)
    text = (
        "Threads of this Home, most recently active first:\n" + "\n".join(lines)
        if lines
        else "This Home has no threads."
    )
    return ToolResult(text=text, attachments=None, data={"threads": facts})


async def read_home_messages_tool(exec_context: ToolExecutionContext) -> ToolResult:
    """The latest messages of the thread's Home, each with its taint merged into the turn."""
    service = exec_context.processing_service
    refusal = await read_home_refusal(
        exec_context.db_context,
        app_config=_app_config(exec_context),
        receives_household_context=(
            service is not None and service.service_config.receives_household_context
        ),
        conversation_id=exec_context.conversation_id,
        subconversation_id=exec_context.subconversation_id,
    )
    if refusal is not None:
        return _error(refusal)
    db = exec_context.db_context
    thread = await db.conversation_threads.get_thread(exec_context.conversation_id)
    assert thread is not None
    app_config = _app_config(exec_context)
    rows = await db.message_history.recent_home_messages(
        conversation_id=thread.home_conversation_id,
        limit=READ_HOME_LIMIT,
        history_taint_epoch=(
            app_config.taint_policy.history_taint_epoch
            if app_config is not None
            else None
        ),
    )
    messages: list[dict[str, object]] = []
    lines: list[str] = []
    for row in rows:
        _merge_fact_taint(exec_context, row.taint_metadata)
        sent_at = row.timestamp.isoformat()
        messages.append({
            "role": row.role,
            "sent_at": sent_at,
            "content": row.content,
        })
        lines.append(f"[{sent_at}] {row.role}: {row.content}")
    text = (
        "Latest messages in Home, oldest first:\n" + "\n\n".join(lines)
        if lines
        else "Home has no messages."
    )
    return ToolResult(text=text, attachments=None, data={"messages": messages})


def _merge_fact_taint(
    exec_context: ToolExecutionContext, metadata: TaintMetadata | None
) -> None:
    """Raise this turn's taint by what one thread fact carries."""
    if exec_context.taint_tracker is None or metadata is None:
        return
    merge_taint_state_into_tracker(
        exec_context.taint_tracker,
        TurnTaintState.from_metadata(
            metadata, preserve_origin=True
        ).with_sensitive_reads_from(metadata),
    )


async def _echoed_outcome(
    exec_context: ToolExecutionContext, echo_message_id: int | None
) -> str | None:
    """The outcome a finished thread echoed, with its taint merged into the turn.

    An echo whose taint is not recorded is left out rather than shown with no
    taint at all.
    """
    if echo_message_id is None:
        return None
    row = await exec_context.db_context.message_history.get_row_by_internal_id(
        echo_message_id
    )
    if row is None or not isinstance(row["content"], str):
        return None
    taint = row["taint_metadata_json"]
    if taint is None:
        return None
    _merge_fact_taint(exec_context, taint)
    return row["content"]


async def start_thread_tool(
    exec_context: ToolExecutionContext,
    target_service_id: str,
    title: str,
    request: str,
    attachment_ids: list[str] | None = None,
    model_tier: str | None = None,
) -> ToolResult:
    """Start a thread of the user's Home on ``target_service_id``."""
    interface_type = delegation_interface_type(exec_context)
    refusal = await thread_start_refusal(
        exec_context.db_context,
        app_config=_app_config(exec_context),
        async_delegation_enabled=delegation_tools_config(
            exec_context
        ).async_delegation_enabled,
        in_script=exec_context.in_script,
        interface_type=exec_context.interface_type,
        conversation_id=exec_context.conversation_id,
        subconversation_id=exec_context.subconversation_id,
        user_id=exec_context.user_id,
    )
    if refusal is not None:
        return _error(refusal)
    title = title.strip()
    request = request.strip()
    if not title or not request:
        return _error("A thread needs a title and a request.")

    target_service, source_service_id, target_error = resolve_delegation_target(
        exec_context, target_service_id, tool_name=TOOL_NAME
    )
    if target_error is not None:
        return target_error
    assert target_service is not None and source_service_id is not None
    # Local imports: the processing package imports the tools package.
    from family_assistant.processing.protocol import (  # noqa: PLC0415
        PollableDelegationService,
    )
    from family_assistant.processing.types import (  # noqa: PLC0415
        ProcessingServiceConfig,
    )

    target_config = target_service.service_config
    if isinstance(target_service, PollableDelegationService) or not isinstance(
        target_config, ProcessingServiceConfig
    ):
        return _error(
            f"Profile '{target_service_id}' runs remotely; a thread can only run "
            "on a local profile."
        )

    try:
        model_selection = resolve_model_selection(
            target_config.tier_eligibility,
            ModelSelectionRequest(tier=model_tier, source="model")
            if model_tier is not None
            else None,
            profile_id=target_config.id,
        )
    except ModelTierNotPermitted as refusal_reason:
        return _error(str(refusal_reason))

    content_parts, attachment_error = await delegation_content_parts(
        exec_context,
        target_service_id=target_service_id,
        user_request=request,
        attachment_ids=attachment_ids,
    )
    if attachment_error is not None:
        return attachment_error

    thread_conversation_id = new_thread_conversation_id(interface_type)
    model_selection = await target_service.resolve_model_selection_for_run(
        model_selection,
        db_context=exec_context.db_context,
        interface_type=interface_type,
        conversation_id=thread_conversation_id,
        subconversation_id=None,
        trigger_content_parts=content_parts,
        acting_user_id=exec_context.user_id,
    )

    owner = exec_context.user_id
    app_config = _app_config(exec_context)
    assert owner is not None and app_config is not None
    home_conversation_id = exec_context.conversation_id
    taint = current_taint_metadata(exec_context)
    delegation_id = f"delegation_{uuid.uuid4().hex}"
    task_id = f"{DELEGATED_PROFILE_RUN_TASK_TYPE}_{uuid.uuid4().hex}"
    turn_id = str(uuid.uuid4())
    now = (exec_context.clock or SystemClock()).now()

    # Recorded on the seed so the files stay part of the thread's history after
    # its first turn, as an uploaded file does.
    seed_attachments = [
        MessageAttachmentMetadata(
            type="attachment_reference", attachment_id=part["attachment_id"]
        )
        for part in content_parts
        if part["type"] == "attachment"
    ]

    review_record = await delegated_run_review_record(exec_context, request=request)

    async def _create(txn: DatabaseTransaction) -> None:
        await create_thread(
            txn,
            thread=NewThread(
                conversation_id=thread_conversation_id,
                home_conversation_id=home_conversation_id,
                interface_type=interface_type,
                owner_user_id=home_owner_id(app_config, owner),
                user_id=owner,
                profile=target_config,
                title=title,
                title_taint=taint,
                source_tool_call_id=exec_context.tool_call_id,
                delegation_id=delegation_id,
            ),
            seed=AssistantRequest(
                text=request,
                taint=taint,
                attachments=seed_attachments,
            ),
            turn_id=turn_id,
            now=now,
        )
        await txn.delegation_runs.create_run({
            "delegation_id": delegation_id,
            "task_id": task_id,
            "source_profile_id": source_service_id,
            "target_service_id": target_service_id,
            "interface_type": interface_type,
            "origin_interface_type": exec_context.interface_type,
            "conversation_id": thread_conversation_id,
            "user_id": owner,
            "user_name": exec_context.user_name,
            "source_turn_id": exec_context.turn_id,
            "source_subconversation_id": None,
            "subconversation_id": None,
            "run_kind": "thread",
            "turn_id": turn_id,
            "request_text": request,
            "content_parts_json": content_parts,
            "taint_state_json": taint,
            "model_selection_json": model_selection.to_json(),
            **review_record,
        })
        await txn.tasks.enqueue(
            task_id=task_id,
            task_type=DELEGATED_PROFILE_RUN_TASK_TYPE,
            payload={
                "delegation_id": delegation_id,
                "interface_type": interface_type,
                "conversation_id": thread_conversation_id,
                "user_name": exec_context.user_name,
            },
            max_retries_override=1,
            priority=TaskPriority.INTERACTIVE,
        )

    await exec_context.db_context.atomic(_create)
    logger.info(
        "Started thread %s of Home %s on profile %s (delegation_id=%s)",
        thread_conversation_id,
        home_conversation_id,
        target_service_id,
        delegation_id,
    )
    # Otherwise clients learn of the thread only when this Home turn ends.
    publisher = getattr(
        exec_context.processing_service, "home_activity_publisher", None
    )
    if publisher is not None:
        await publisher.publish_activity(
            home_conversation_id, user_id=owner, reason="thread_started"
        )
    return ToolResult(
        text=(
            f"Started the thread '{title}' on '{target_service_id}'. Tell the user "
            "you have started a thread for it and end your turn; its progress "
            "and answers appear in the thread."
        ),
        attachments=None,
        data={
            "thread_conversation_id": thread_conversation_id,
            "delegation_id": delegation_id,
            "target_service_id": target_service_id,
            "title": title,
            "status": "queued",
        },
    )
