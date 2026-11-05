"""Posting a note into another of the acting user's own conversations.

``post_to_conversation`` lets one conversation's assistant hand something to
another conversation the same user owns -- a thread telling Home something
mid-way, or Home passing a finding into one of its threads -- and wakes the
assistant there to act on it.

The note crosses through the conversation boundary as an assistant request
(machine-authored data, never the user's words, carrying the posting turn's
taint), and the woken turn runs on the run machinery a thread's first turn
uses: a delegation-admitted run at the top level of the target conversation,
under the profile the user talks to there, that wakes nobody when it ends. The
woken turn therefore starts as tainted as the turn that posted, exactly as a
delegated turn does. See docs/design/cross-conversation-notes.md.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from family_assistant.actions import (
    ActionType,
    WakeLlmProfileError,
    assert_wake_llm_allowed,
)
from family_assistant.llm.messages import MessageAttachmentMetadata
from family_assistant.llm.model_selection import (
    ModelTierNotPermitted,
    resolve_model_selection,
)
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage.tasks import TaskPriority
from family_assistant.threads.boundary import (
    AssistantRequest,
    BoundaryDestination,
    copy_across_boundary,
)
from family_assistant.threads.home import find_home
from family_assistant.tools.communication import (
    UnknownMessageTargetError,
    resolve_send_message_target,
)
from family_assistant.tools.services import (
    DELEGATED_PROFILE_RUN_TASK_TYPE,
    current_taint_metadata,
    delegation_content_parts,
    delegation_tools_config,
    resolve_delegation_target,
)
from family_assistant.tools.types import ToolResult
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.storage.database import DatabaseTransaction
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)

TOOL_NAME = "post_to_conversation"
HOME_TARGET = "home"
NOTE_INTERFACE_TYPE = "web"
"""Notes reach web conversations only: Home and its threads live there, and
the woken turn runs on the web thread surface so the user watches it live."""


def note_prompt(source_conversation_id: str, content: str) -> str:
    """The woken turn's prompt: the note, framed as coming from elsewhere.

    Framed by fixed text, so the target's assistant neither reads it as the
    user's words nor as something said in its own conversation, and knows
    where to post back if it is asked to.
    """
    return (
        "A note posted into this conversation by the assistant in another of "
        f"the user's conversations ({source_conversation_id}). Act on it as "
        "the user would want here; your reply appears in this conversation.\n\n"
        f"{content}"
    )


CROSS_CONVERSATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Post a note into another of the current user's own conversations, such "
                "as their Home or one of their threads. The assistant there is woken to "
                "act on it, and its reply appears in that conversation. Use it when the "
                "user asks you to pass something to another conversation, or when a "
                "result here belongs in a conversation that is waiting for it. It cannot "
                "reach other people's conversations; use send_message_to_user to "
                "message someone else. Pass 'home' for the user's Home, or a "
                "conversation_id you were given (list_threads lists Home's threads)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_conversation_id": {
                        "type": "string",
                        "description": (
                            "'home' for the user's Home, or the id of another "
                            "conversation this user owns."
                        ),
                    },
                    "message_content": {
                        "type": "string",
                        "description": (
                            "The note: what the assistant there should know or do."
                        ),
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional attachment UUIDs of this user's to pass along."
                        ),
                    },
                },
                "required": ["target_conversation_id", "message_content"],
            },
        },
    },
]


def _error(text: str) -> ToolResult:
    return ToolResult(text=f"Error: {text}", attachments=None)


def _app_config(exec_context: ToolExecutionContext) -> AppConfig | None:
    service = exec_context.processing_service
    return getattr(service, "app_config", None) if service is not None else None


async def _resolve_target_id(
    exec_context: ToolExecutionContext, target: str, user_id: str
) -> str | ToolResult:
    if target.strip().lower() != HOME_TARGET:
        return target.strip()
    app_config = _app_config(exec_context)
    if app_config is None or not app_config.conversation_threads.enabled:
        return _error("This user has no Home to post into.")
    home = await find_home(
        exec_context.db_context,
        app_config,
        user_id=user_id,
        interface_type=exec_context.interface_type,
    )
    if home is None:
        return _error("This user has no Home to post into.")
    return home


def _same_owner(
    app_config: AppConfig | None, owner_ids: set[str], user_id: str
) -> bool:
    """Whether every owner of the target is the acting user, aliases resolved."""
    if app_config is None:
        return owner_ids == {user_id}
    resolver = UserIdentityResolver(app_config)
    canonical = resolver.canonicalize_owner_id(user_id)
    return {resolver.canonicalize_owner_id(owner) for owner in owner_ids} == {canonical}


async def _target_profile_id(
    exec_context: ToolExecutionContext, conversation_id: str
) -> str | None:
    """The profile the user talks to in the target, which the woken turn runs on.

    History windows are scoped by profile, so this is also the only profile
    whose turns would see the note. A thread nobody has replied in yet has
    only its seed, so its own profile stands in.
    """
    db = exec_context.db_context
    profile_id = await db.message_history.get_latest_user_profile_id(conversation_id)
    if profile_id is not None:
        return profile_id
    thread = await db.conversation_threads.get_thread(conversation_id)
    return thread.profile_id if thread is not None else None


async def _owned_target(
    exec_context: ToolExecutionContext, target_conversation_id: str, user_id: str
) -> str | ToolResult:
    """The target's conversation id, if it is another web conversation of the user's."""
    resolved = await _resolve_target_id(
        exec_context, str(target_conversation_id), user_id
    )
    if isinstance(resolved, ToolResult):
        return resolved
    if resolved == exec_context.conversation_id:
        return _error(
            "That is this conversation. Reply here directly instead of posting a note."
        )
    not_owned = _error(
        f"{resolved} is not one of this user's conversations. Use "
        "send_message_to_user to message someone else."
    )
    try:
        interface_type = await resolve_send_message_target(exec_context, resolved)
    except UnknownMessageTargetError:
        return not_owned
    owner_ids = (
        await exec_context.db_context.message_history.get_conversation_owner_ids(
            resolved
        )
    )
    if not _same_owner(_app_config(exec_context), owner_ids, user_id):
        logger.warning(
            "Refused %s from %s into %s: owners %s are not the acting user",
            TOOL_NAME,
            exec_context.conversation_id,
            resolved,
            sorted(owner_ids),
        )
        return not_owned
    if interface_type != NOTE_INTERFACE_TYPE:
        return _error(
            f"{resolved} is a {interface_type} conversation; notes can only be "
            "posted into web conversations."
        )
    return resolved


async def post_to_conversation_tool(
    exec_context: ToolExecutionContext,
    target_conversation_id: str,
    message_content: str,
    attachment_ids: list[str] | None = None,
) -> ToolResult:
    """Post a note into another conversation the acting user owns, and wake it."""
    user_id = exec_context.user_id
    if not user_id:
        return _error("Posting to another conversation needs a signed-in user.")
    content = message_content.strip()
    if not content:
        return _error("The note is empty.")
    try:
        assert_wake_llm_allowed(ActionType.WAKE_LLM, exec_context.allow_wake_llm)
    except WakeLlmProfileError as err:
        return _error(str(err))
    if (
        exec_context.in_script
        or not delegation_tools_config(exec_context).async_delegation_enabled
    ):
        return _error(
            "Posting a note needs asynchronous delegation, which is not available here."
        )

    target = await _owned_target(exec_context, target_conversation_id, user_id)
    if isinstance(target, ToolResult):
        return target
    profile_id = await _target_profile_id(exec_context, target)
    if profile_id is None:
        return _error(f"No assistant profile has talked to the user in {target}.")

    # Waking the target's profile hands it this turn's work, so it is admitted
    # as a delegation to that profile is.
    target_service, source_service_id, target_error = resolve_delegation_target(
        exec_context, profile_id, tool_name=TOOL_NAME
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
            f"{target} runs on the remote profile '{profile_id}', which cannot be "
            "woken by a note."
        )

    prompt = note_prompt(exec_context.conversation_id, content)
    content_parts, attachment_error = await delegation_content_parts(
        exec_context,
        target_service_id=profile_id,
        user_request=prompt,
        attachment_ids=attachment_ids,
    )
    if attachment_error is not None:
        return attachment_error
    try:
        model_selection = resolve_model_selection(
            target_config.tier_eligibility, None, profile_id=target_config.id
        )
    except ModelTierNotPermitted as refusal:
        return _error(str(refusal))
    model_selection = await target_service.resolve_model_selection_for_run(
        model_selection,
        db_context=exec_context.db_context,
        interface_type=NOTE_INTERFACE_TYPE,
        conversation_id=target,
        subconversation_id=None,
        trigger_content_parts=content_parts,
        acting_user_id=user_id,
    )

    taint = current_taint_metadata(exec_context)
    delegation_id = f"delegation_{uuid.uuid4().hex}"
    task_id = f"{DELEGATED_PROFILE_RUN_TASK_TYPE}_{uuid.uuid4().hex}"
    turn_id = str(uuid.uuid4())
    now = (exec_context.clock or SystemClock()).now()
    request = AssistantRequest(
        text=prompt,
        taint=taint,
        attachments=[
            MessageAttachmentMetadata(
                type="attachment_reference", attachment_id=part["attachment_id"]
            )
            for part in content_parts
            if part["type"] == "attachment"
        ],
    )

    async def _post(txn: DatabaseTransaction) -> None:
        await copy_across_boundary(
            txn,
            destination=BoundaryDestination(
                interface_type=NOTE_INTERFACE_TYPE,
                conversation_id=target,
                profile=target_config,
                user_id=user_id,
            ),
            payload=request,
            turn_id=turn_id,
            now=now,
        )
        await txn.delegation_runs.create_run({
            "delegation_id": delegation_id,
            "task_id": task_id,
            "source_profile_id": source_service_id,
            "target_service_id": profile_id,
            "interface_type": NOTE_INTERFACE_TYPE,
            "origin_interface_type": exec_context.interface_type,
            "conversation_id": target,
            "user_id": user_id,
            "user_name": exec_context.user_name,
            "source_turn_id": exec_context.turn_id,
            "source_subconversation_id": None,
            "subconversation_id": None,
            # A turn at the top level of the target that wakes nobody when it
            # ends: the run a thread's first turn is.
            "run_kind": "thread",
            "turn_id": turn_id,
            "request_text": prompt,
            "content_parts_json": content_parts,
            "taint_state_json": taint,
            "model_selection_json": model_selection.to_json(),
        })
        await txn.tasks.enqueue(
            task_id=task_id,
            task_type=DELEGATED_PROFILE_RUN_TASK_TYPE,
            payload={
                "delegation_id": delegation_id,
                "interface_type": NOTE_INTERFACE_TYPE,
                "conversation_id": target,
                "user_name": exec_context.user_name,
            },
            max_retries_override=1,
            priority=TaskPriority.INTERACTIVE,
        )

    await exec_context.db_context.atomic(_post)
    logger.info(
        "Posted a note from %s into %s on profile %s (delegation_id=%s)",
        exec_context.conversation_id,
        target,
        profile_id,
        delegation_id,
    )
    return ToolResult(
        text=(
            f"Posted the note into conversation {target}; its assistant has been "
            "woken to act on it and will reply there."
        ),
        attachments=None,
        data={
            "conversation_id": target,
            "delegation_id": delegation_id,
            "profile_id": profile_id,
            "status": "queued",
        },
    )
