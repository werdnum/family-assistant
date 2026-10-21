"""
Shared action execution logic for both event listeners and scheduled tasks.
"""

import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from family_assistant.security.definition_records import (
    DefinitionArtifactKind,
    DefinitionGateOutcome,
    authoring_taint_state,
    register_definition_write,
    script_invocation_content,
    stamp_callback_definition,
    stamp_definition,
)
from family_assistant.security.taint import TurnTaintTracker
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.threads.home import ensure_web_home

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.storage.database import DatabaseExecutor
    from family_assistant.task_worker import LlmCallbackPayload

logger = logging.getLogger(__name__)

# Restricted profile that event-triggered wakes run under, whether the event
# listener's action is wake_llm or a script that calls wake_llm(). Event content
# is untrusted (attacker-influenced email/webhook data), and the woken LLM is the
# injectable component, so it must not run under a full-trust profile.
EVENT_HANDLER_PROFILE_ID = "event_handler"


class ActionType(StrEnum):
    """Action types supported by the system."""

    WAKE_LLM = "wake_llm"
    SCRIPT = "script"


class WakeLlmProfileError(RuntimeError):
    """Raised when a profile that may not wake the LLM attempts to.

    A woken turn does not necessarily run under the profile that scheduled it.
    Schedule automations, ``schedule_action`` and a script's built-in
    ``wake_llm()`` stamp their originating profile and ``handle_llm_callback``
    resolves it, but event-triggered wakes -- an event listener's wake_llm
    action, or ``wake_llm()`` from an event listener's script -- deliberately
    route to the restricted ``event_handler`` profile because the triggering
    event is untrusted.

    Either way a confined profile must not be able to enqueue a wake: via an
    event it would escalate to ``event_handler``, and via a stamped wake
    it would fail at fire time, when ``handle_llm_callback`` re-checks the flag.
    Profiles that must stay confined set ``allow_wake_llm=False`` and are refused
    here, at creation, instead of either.
    """


def assert_wake_llm_allowed(
    action_type: ActionType | str,
    allow_wake_llm: bool,
) -> None:
    """Refuse a wake_llm action from a profile that disallows waking the LLM.

    No-op for scripts and for profiles with ``allow_wake_llm=True`` (the default,
    which preserves existing behavior for the default assistant, event handlers,
    and other full-capability profiles). Raises ``WakeLlmProfileError`` when a
    confined profile (``allow_wake_llm=False``) attempts a wake_llm action.
    """
    if ActionType(action_type) != ActionType.WAKE_LLM:
        return
    if not allow_wake_llm:
        raise WakeLlmProfileError(
            "This profile is not permitted to wake the LLM (allow_wake_llm is "
            "disabled). A woken turn would not stay inside this profile's "
            'confinement. Use action_type="script" and keep results in data '
            "(notes) instead of waking the assistant."
        )


# Values of an action config's ``conversation`` key, naming where its wakes
# land: the conversation the automation was created in, the owner's Home, or a
# fresh conversation per firing. A script action takes source or home, for its
# wake_llm() calls and failure notices; a script asks for a new conversation
# per call instead.
#
# An absent key means source. That is what keeps automations created before
# Home existed where they were: a new automation does not rely on the absence,
# but has its destination written into its config when it is created (see
# with_default_wake_destination).
WAKE_CONVERSATION_SOURCE = "source"
WAKE_CONVERSATION_HOME = "home"
WAKE_CONVERSATION_NEW = "new"
WAKE_CONVERSATION_DESTINATIONS = (
    WAKE_CONVERSATION_HOME,
    WAKE_CONVERSATION_SOURCE,
    WAKE_CONVERSATION_NEW,
)

# Surfaces whose owners have a web Home: voice sessions are saved as web
# conversations, and the iOS app is a web client. A Telegram chat is already
# its own Home, so its wakes stay in it.
HOME_INTERFACE_TYPES = frozenset({"web", "voice"})


class WakeDestinationError(ValueError):
    """Raised when a wake names a destination it cannot have.

    Only web conversations exist as separate threads: a Telegram chat is one
    conversation, so there is nothing new to open there and no Home apart from
    it. A new conversation or a Home also needs an owner, because a
    conversation with no owning user row is listed for every user and a Home
    is one user's.
    """


def wake_destination(action_config: Mapping[str, object]) -> str:
    """The destination an action config names, ``source`` when it names none."""
    destination = action_config.get("conversation", WAKE_CONVERSATION_SOURCE)
    if (
        not isinstance(destination, str)
        or destination not in WAKE_CONVERSATION_DESTINATIONS
    ):
        raise WakeDestinationError(
            "action_config.conversation must be 'home', 'source' or 'new', "
            f"got {destination!r}."
        )
    return destination


def wants_new_conversation(action_config: Mapping[str, object]) -> bool:
    """Whether a wake_llm action config asks for a new conversation per firing."""
    return wake_destination(action_config) == WAKE_CONVERSATION_NEW


def home_owner_for(
    action_config: Mapping[str, object], owner_user_id: str | None
) -> str | None:
    """The user whose Home an action's wakes land in, or None for elsewhere."""
    if wake_destination(action_config) != WAKE_CONVERSATION_HOME:
        return None
    return owner_user_id


def assert_new_conversation_allowed(
    *, interface_type: str, owner_user_id: str | None
) -> None:
    """Refuse a new-conversation wake outside the web interface or without an owner."""
    if interface_type != "web":
        raise WakeDestinationError(
            "Starting a new conversation is only supported for automations "
            f"created in the web app, not from {interface_type}."
        )
    if owner_user_id is None:
        raise WakeDestinationError(
            "Starting a new conversation needs a recorded owner, and this "
            "automation has none."
        )


def assert_home_allowed(
    *, interface_type: str, owner_user_id: str | None, threads_enabled: bool
) -> None:
    """Refuse a Home destination where the owner has no web Home to land in."""
    if not threads_enabled:
        raise WakeDestinationError(
            "Delivering to Home needs conversation threads, which are not "
            "enabled; use 'source' instead."
        )
    if interface_type not in HOME_INTERFACE_TYPES:
        raise WakeDestinationError(
            "Delivering to Home is only supported for the web app, the iOS app "
            f"and voice, not from {interface_type}, whose chat is already its "
            "Home; use 'source' instead."
        )
    if owner_user_id is None:
        raise WakeDestinationError(
            "Delivering to Home needs a recorded owner, and this has none; use "
            "'source' instead."
        )


def threads_enabled(processing_service: object | None) -> bool:
    """Whether conversation threads, and so Homes to deliver into, are on."""
    # Read by attribute, and compared with ``is True``, because tool tests
    # hand in stand-in services whose attributes are mocks.
    app_config = getattr(processing_service, "app_config", None)
    threads = getattr(app_config, "conversation_threads", None)
    return getattr(threads, "enabled", False) is True


async def default_wake_destination(
    db: "DatabaseExecutor",
    *,
    conversation_id: str,
    interface_type: str,
    owner_user_id: str | None,
    threads_enabled: bool,
) -> str:
    """Where a wake created now lands when its author names no destination.

    A web or voice owner's wakes land in their Home, except those created in
    a thread: a thread's reminder or callback belongs to its work, and
    arriving there is what reopens it.
    """
    if not (
        threads_enabled
        and interface_type in HOME_INTERFACE_TYPES
        and owner_user_id is not None
    ):
        return WAKE_CONVERSATION_SOURCE
    if await db.conversation_threads.get_thread(conversation_id) is not None:
        return WAKE_CONVERSATION_SOURCE
    return WAKE_CONVERSATION_HOME


async def with_default_wake_destination(
    db: "DatabaseExecutor",
    action_config: Mapping[str, object],
    *,
    conversation_id: str,
    interface_type: str,
    owner_user_id: str | None,
    threads_enabled: bool,
) -> dict[str, object]:
    """The config a new automation is stored with, its destination written in.

    Writing the default down, rather than reading an absent key as Home, is
    what lets an absent key keep meaning source for automations stored before
    Home existed. A source default is left absent, which means the same.
    """
    if "conversation" in action_config:
        return dict(action_config)
    destination = await default_wake_destination(
        db,
        conversation_id=conversation_id,
        interface_type=interface_type,
        owner_user_id=owner_user_id,
        threads_enabled=threads_enabled,
    )
    if destination == WAKE_CONVERSATION_SOURCE:
        return dict(action_config)
    return {**action_config, "conversation": destination}


def carry_wake_destination(
    new_config: Mapping[str, object],
    existing_config: Mapping[str, object] | None,
) -> dict[str, object]:
    """An updated action config, keeping the stored destination unless it names one.

    Without this, editing an automation's prompt would quietly move it back
    to its source conversation, since an absent key means source.
    """
    if (
        "conversation" in new_config
        or not existing_config
        or "conversation" not in existing_config
    ):
        return dict(new_config)
    return {**new_config, "conversation": existing_config["conversation"]}


def validate_wake_destination(
    action_type: ActionType | str,
    action_config: Mapping[str, object],
    *,
    interface_type: str,
    owner_user_id: str | None,
    threads_enabled: bool,
) -> None:
    """Check an action's conversation choice at creation or update.

    The worker resolves the same choice when the wake fires; this gives the
    author the error while they can still fix it.
    """
    destination = wake_destination(action_config)
    if destination == WAKE_CONVERSATION_HOME:
        assert_home_allowed(
            interface_type=interface_type,
            owner_user_id=owner_user_id,
            threads_enabled=threads_enabled,
        )
    elif destination == WAKE_CONVERSATION_NEW:
        if ActionType(action_type) == ActionType.SCRIPT:
            raise WakeDestinationError(
                "A script action takes 'home' or 'source'. To start a new "
                "conversation, call wake_llm(..., new_conversation=True) in "
                "the script."
            )
        assert_new_conversation_allowed(
            interface_type=interface_type, owner_user_id=owner_user_id
        )


async def resolve_home_target(
    db: "DatabaseExecutor",
    app_config: "AppConfig | None",
    *,
    interface_type: str,
    conversation_id: str,
    home_owner_user_id: str,
    now: datetime,
) -> tuple[str, str]:
    """The ``(interface_type, conversation_id)`` a Home-bound wake fires in.

    Resolved when the wake fires, so it lands in the Home the owner has then,
    designating one if they have none yet. Where no Home applies any more --
    threads switched off since the wake was created -- it fires in the
    conversation it was created in, which is where it would have gone before
    Home existed.
    """
    if app_config is None or not app_config.conversation_threads.enabled:
        logger.info(
            "Wake for %s's Home fires in its source conversation %s:%s: "
            "conversation threads are not enabled.",
            home_owner_user_id,
            interface_type,
            conversation_id,
        )
        return interface_type, conversation_id
    if interface_type not in HOME_INTERFACE_TYPES:
        logger.warning(
            "Wake for %s's Home fires in its source conversation %s:%s: "
            "%s has no web Home.",
            home_owner_user_id,
            interface_type,
            conversation_id,
            interface_type,
        )
        return interface_type, conversation_id
    home = await ensure_web_home(db, app_config, user_id=home_owner_user_id, now=now)
    return "web", home


async def execute_action(
    db_ctx: Database,
    action_type: ActionType,
    # ast-grep-ignore: no-dict-any - action config has varying keys per action type
    action_config: dict[str, Any],
    conversation_id: str,
    interface_type: str = "telegram",
    user_name: str | None = None,  # Added user_name
    # ast-grep-ignore: no-dict-any - arbitrary context data passed through to action handlers
    context: dict[str, Any] | None = None,
    scheduled_at: datetime | None = None,
    recurrence_rule: str | None = None,
    processing_profile_id: str | None = None,
    created_by_user_id: str | None = None,
    allow_wake_llm: bool = True,
    tool_call_review_trigger_type: str | None = None,
    tool_call_review_trigger_definition: str | None = None,
    tool_call_review_trigger_payload_present: bool | None = None,
    definition_taint_tracker: TurnTaintTracker | None = None,
    definition_gate: DefinitionGateOutcome | None = None,
) -> None:
    """
    Execute an action. Used by both event listeners and scheduled tasks.

    Args:
        db_ctx: Database context
        action_type: Type of action to execute
        action_config: Configuration for the action
        conversation_id: Conversation to execute in
        interface_type: Interface type (telegram, web, etc)
        user_name: Optional user name context for the action
        context: Additional context (e.g., event data, trigger info)
        scheduled_at: When to execute the action (None for immediate)
        recurrence_rule: RRULE for recurring tasks (None for one-time)
        processing_profile_id: The profile the resulting turn or script runs
            under. Scripts execute under their creating profile so validation and
            execution agree. wake_llm carries it too -- handle_llm_callback
            resolves it fail-loud -- except for event listeners, which the event
            processor stamps with the restricted event_handler profile instead,
            because the triggering event is untrusted.
        created_by_user_id: Creating user for script actions; confirm-gated
            tool calls from the script are addressed to this user.
        definition_taint_tracker: The authoring turn's taint tracker, stamped
            onto the enqueued definition record. Absent means the write cannot
            prove its turn was clean, so the definition stamps unknown_external
            and its firings stay fail-closed.
        definition_gate: How the gate that admitted the enqueuing call resolved,
            recorded on the enqueued definition record. Absent means no gate
            examined the write, which cures nothing.
        allow_wake_llm: Whether the acting profile may wake the LLM. When False,
            a wake_llm action is refused loudly (see assert_wake_llm_allowed)
            rather than being enqueued for a turn that would escape the profile's
            confinement.
    """
    if context is None:
        context = {}

    # A woken turn does not stay inside a confined profile, so such a profile
    # must not be able to enqueue one at all.
    assert_wake_llm_allowed(action_type, allow_wake_llm)

    if action_type == ActionType.WAKE_LLM:
        # Prepare callback context
        callback_context = {
            "trigger": context.get("trigger", "Scheduled action"),
            **context,
        }

        # Include any wake context from config
        if "context" in action_config:
            callback_context["message"] = action_config["context"]

        # The uuid suffix, not just the millisecond stamp: one event can
        # match several listeners, and their enqueues land well inside the
        # same millisecond. task_id is unique, so a bare stamp collides.
        task_id = f"action_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

        payload: LlmCallbackPayload = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "callback_context": callback_context,
            "scheduling_timestamp": datetime.now(UTC).isoformat(),
            "tool_call_review_trigger_type": (
                tool_call_review_trigger_type or "scheduled_callback"
            ),
            "tool_call_review_trigger_definition": (
                tool_call_review_trigger_definition
                if tool_call_review_trigger_type is not None
                else (
                    str(action_config["context"])
                    if isinstance(action_config.get("context"), str)
                    else None
                )
            ),
            "tool_call_review_trigger_payload_present": (
                tool_call_review_trigger_payload_present
                if tool_call_review_trigger_payload_present is not None
                else False
            ),
        }
        payload["tool_call_review_definition_record"] = stamp_callback_definition(
            payload["tool_call_review_trigger_definition"],
            tracker=definition_taint_tracker,
            gate_outcome=definition_gate,
        )
        if user_name:
            payload["user_name"] = user_name
        if created_by_user_id is not None:
            payload["created_by_user_id"] = created_by_user_id
        # Honor the wake's execution profile (event listeners route to
        # event_handler; one-time schedules carry their originating profile).
        if processing_profile_id is not None:
            payload["processing_profile_id"] = processing_profile_id
        if wants_new_conversation(action_config):
            payload["new_conversation"] = True
        home_owner = home_owner_for(action_config, created_by_user_id)
        if home_owner is not None:
            payload["home_owner_user_id"] = home_owner

        register_definition_write(
            definition_gate,
            payload["tool_call_review_definition_record"],
            kind=DefinitionArtifactKind.TASK_PAYLOAD,
            artifact_id=task_id,
        )
        await db_ctx.tasks.enqueue(
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_at,
            recurrence_rule=recurrence_rule,
            priority=TaskPriority.INTERACTIVE,
        )

    elif action_type == ActionType.SCRIPT:
        task_id = f"script_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

        script_payload: dict[str, object] = {
            "config": action_config,
            "conversation_id": conversation_id,
            "interface_type": interface_type,
            **context,
        }
        if action_config.get("script_code"):
            script_payload["script_code"] = action_config["script_code"]
        elif action_config.get("script_name"):
            script_payload["script_name"] = action_config["script_name"]
            if action_config.get("parameters"):
                script_payload["script_parameters"] = action_config["parameters"]
        # A one-shot script action is stored intent with no definition table:
        # the action config the payload carries *is* the definition, so it
        # stamps here like any other executable-persistence write. An action
        # fired from a durable automation or listener stamps this too, and
        # resolution ignores it in favour of that row -- this record is written
        # by the firing, which has no authoring turn to speak for.
        script_payload["tool_call_review_definition_record"] = stamp_definition(
            content=script_invocation_content(action_config),
            taint_state=authoring_taint_state(definition_taint_tracker),
            gate_outcome=definition_gate,
        ).to_dict()
        if user_name:
            script_payload["user_name"] = user_name
        if processing_profile_id is not None:
            script_payload["processing_profile_id"] = processing_profile_id
        if created_by_user_id is not None:
            script_payload["created_by_user_id"] = created_by_user_id

        register_definition_write(
            definition_gate,
            script_payload["tool_call_review_definition_record"],
            kind=DefinitionArtifactKind.TASK_PAYLOAD,
            artifact_id=task_id,
        )
        await db_ctx.tasks.enqueue(
            task_id=task_id,
            task_type="script_execution",
            payload=script_payload,
            scheduled_at=scheduled_at,
            recurrence_rule=recurrence_rule,
            priority=TaskPriority.INTERACTIVE,
        )
    else:
        raise ValueError(f"Unknown action type: {action_type}")
