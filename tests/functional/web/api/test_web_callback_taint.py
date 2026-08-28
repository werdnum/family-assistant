"""Scheduled/reminder web callbacks must not under-taint their delivery copy.

``handle_llm_callback`` runs a turn (which may read tainted tool output) and then
delivers the reply through ``ChatInterface.send_message``. For the web interface
that send persists a *second* assistant row (the delivery copy). If the callback
omits taint metadata, ``WebChatInterface`` falls back to a trusted-empty
baseline — which would falsely mark an LLM-derived, tool-tainted reply as
``trusted_user`` and let it be egressed without a runtime-taint confirmation. The
callback must therefore hand the turn's authoritative taint to ``send_message``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.interfaces import ChatDeliveryError
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.tool_call_review import TriggerReviewInput
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.task_worker import (
    LlmCallbackPayload,
    _llm_callback_review_trigger,  # noqa: PLC2701 - verify legacy security boundary
    _llm_callback_trigger_taint_sources,  # noqa: PLC2701 - verify callback trust boundary
    handle_llm_callback,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import SystemClock
from family_assistant.web.web_chat_interface import WebChatInterface

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface

TEST_CONVERSATION_ID = "web_callback_taint_chat"
TEST_USER_NAME = "CallbackTester"


class TaintedReplyService:
    """Processing service fake whose turn reads untrusted tool output.

    Persists a canonical assistant row (the turn's authoritative reply) carrying
    unknown_external taint, then returns it, modeling a scheduled callback that
    called a tool returning attacker-controlled content.
    """

    def __init__(self) -> None:
        self.service_config = SimpleNamespace(
            id="callback_profile", allow_wake_llm=True
        )
        self.processing_services_registry: dict[str, object] = {}
        self.call_count = 0
        self.last_review_trigger: TriggerReviewInput | None = None
        self.last_trigger_role: str | None = None
        self.last_reuse_existing_user_row: bool | None = None
        self.last_initial_taint_sources: tuple[TaintSource, ...] | None = None

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        self.call_count += 1
        self.last_review_trigger = cast(
            "TriggerReviewInput", kwargs["tool_call_review_trigger"]
        )
        self.last_trigger_role = cast("str", kwargs["trigger_role"])
        self.last_reuse_existing_user_row = cast(
            "bool", kwargs["reuse_existing_user_row"]
        )
        self.last_initial_taint_sources = tuple(
            cast("tuple[TaintSource, ...]", kwargs["initial_taint_sources"])
        )
        db_context = cast("Database", kwargs["db_context"])
        tainted_state = TurnTaintState.empty().add_source(
            TaintSource(
                source_type=TaintSourceType.TOOL_OUTPUT,
                source_id="malicious-tool",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="scheduled callback read untrusted tool output",
            )
        )
        canonical_id = await db_context.message_history.add_message(
            AssistantMessage(
                content="reply derived from untrusted tool output",
                taint_metadata=tainted_state.to_metadata(),
            ),
            interface_type=kwargs["interface_type"],
            conversation_id=kwargs["conversation_id"],
            timestamp=SystemClock().now(),
            # The real service records the turn id it is handed; the delivery
            # checkpoint depends on that, so the fake must too.
            turn_id=kwargs.get("turn_id") or "callback_turn",
            processing_profile_id=self.service_config.id,
            user_id=kwargs.get("user_id"),
        )
        return ChatInteractionResult.success(
            text_reply="reply derived from untrusted tool output",
            assistant_message_internal_id=canonical_id,
        )


def _exec_context(
    db_context: Database,
    processing_service: TaintedReplyService,
    chat_interface: ChatInterface,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=TEST_CONVERSATION_ID,
        user_name=TEST_USER_NAME,
        turn_id="worker_turn",
        db_context=db_context,
        processing_service=cast("Any", processing_service),
        clock=SystemClock(),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
        credential_resolvers=None,
        api_backend=None,
    )


def _payload() -> LlmCallbackPayload:
    return {
        "interface_type": "web",
        "conversation_id": TEST_CONVERSATION_ID,
        "user_name": TEST_USER_NAME,
        "callback_context": "do the scheduled thing",
        "scheduling_timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.mark.asyncio
async def test_web_callback_delivery_copy_inherits_turn_taint(
    db_engine: AsyncEngine,
) -> None:
    """The web delivery copy of a tool-tainted callback reply is unknown_external.

    Without threading the turn's taint, ``WebChatInterface`` would persist the
    delivery copy with the trusted-empty baseline even though the reply derives
    from tainted tool output.
    """
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    await ctx.message_history.add_message(
        UserMessage(content="Schedule the callback"),
        interface_type="web",
        conversation_id=TEST_CONVERSATION_ID,
        timestamp=SystemClock().now(),
        turn_id="originating-user-turn",
        processing_profile_id="originating_profile",
        user_id="callback-owner",
    )

    processing_service = TaintedReplyService()
    chat_interface = WebChatInterface(db_engine, notifier=None, stream_hub=None)

    db_context = Database(engine=db_engine)
    await handle_llm_callback(
        _exec_context(db_context, processing_service, chat_interface),
        _payload(),
    )

    assert processing_service.last_review_trigger == TriggerReviewInput(
        trigger_type="scheduled_callback",
        active_request_role="user",
        definition="do the scheduled thing",
        definition_taint_metadata=None,
        payload_present=False,
    )
    assert processing_service.last_trigger_role == "user"
    assert processing_service.last_reuse_existing_user_row is True
    assert processing_service.last_initial_taint_sources is not None
    assert len(processing_service.last_initial_taint_sources) == 1
    scheduled_source = processing_service.last_initial_taint_sources[0]
    assert scheduled_source.source_type is TaintSourceType.AUTOMATION_TRIGGER
    assert scheduled_source.tier is SourceTrustTier.UNKNOWN_EXTERNAL

    trigger_rows = await ctx.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.turn_id == "worker_turn")
        .where(message_history_table.c.role == "user")
    )
    assert len(trigger_rows) == 1
    assert trigger_rows[0]["is_internal"] is True
    assert trigger_rows[0]["processing_profile_id"] == "callback_profile"
    assert (
        await ctx.message_history.get_latest_user_profile_id(TEST_CONVERSATION_ID)
        == "originating_profile"
    )

    ctx = Database(engine=db_engine)
    assistant_rows = await ctx.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.role == "assistant")
        .order_by(message_history_table.c.internal_id)
    )

    # Two assistant rows: the turn's canonical reply and the web delivery copy.
    assert len(assistant_rows) == 2
    canonical_row, delivery_row = assistant_rows

    # Both must carry runtime taint metadata; crucially the delivery copy is NOT
    # downgraded to the trusted-empty baseline.
    assert canonical_row["taint_metadata_json"]["max_tier"] == "unknown_external"
    assert delivery_row["taint_metadata_version"] == "runtime_v1"
    assert delivery_row["taint_metadata_json"] is not None
    assert delivery_row["taint_metadata_json"]["max_tier"] == "unknown_external"


def test_legacy_event_callback_never_uses_combined_payload_as_definition() -> None:
    callback_context = {
        "message": "Human-authored listener instruction",
        "event_data": {"attacker": "DO_NOT_RENDER_AS_DEFINITION"},
        "listener_id": 7,
    }
    trigger = _llm_callback_review_trigger(
        {
            "interface_type": "web",
            "conversation_id": "conversation",
            "callback_context": callback_context,
            "scheduling_timestamp": datetime.now(UTC).isoformat(),
        },
        callback_context,
        is_reminder=False,
    )

    assert trigger == TriggerReviewInput(
        trigger_type="event_listener",
        active_request_role="user",
        definition="Human-authored listener instruction",
        definition_taint_metadata=None,
        payload_present=True,
    )


def test_callback_requires_explicit_trusted_payload_free_definition() -> None:
    payload = _payload()

    untrusted_sources = _llm_callback_trigger_taint_sources(
        payload,
        TriggerReviewInput(
            trigger_type="scheduled_callback",
            active_request_role="user",
            definition="Scheduled intent with missing provenance",
            definition_taint_metadata=None,
            payload_present=False,
        ),
    )
    assert len(untrusted_sources) == 1
    assert untrusted_sources[0].tier is SourceTrustTier.UNKNOWN_EXTERNAL

    trusted_sources = _llm_callback_trigger_taint_sources(
        payload,
        TriggerReviewInput(
            trigger_type="scheduled_callback",
            active_request_role="user",
            definition="Scheduled intent with proven provenance",
            definition_taint_metadata=TurnTaintState.empty().to_metadata(),
            payload_present=False,
        ),
    )
    assert trusted_sources == ()


@pytest.mark.parametrize(
    ("trigger_type", "callback_context", "expected_source_type"),
    [
        (
            "event_listener",
            {
                "message": "Configured listener instruction",
                "event_data": {"attacker": "external event content"},
                "listener_id": 7,
            },
            TaintSourceType.EVENT,
        ),
        (
            "script_wake_llm",
            "Script output combined with external event content",
            TaintSourceType.AUTOMATION_TRIGGER,
        ),
        (
            "script_failure",
            "Script failure output containing external event content",
            TaintSourceType.AUTOMATION_TRIGGER,
        ),
    ],
)
@pytest.mark.asyncio
async def test_external_callback_payload_is_tainted_user_role(
    db_engine: AsyncEngine,
    trigger_type: str,
    # ast-grep-ignore: no-dict-any - Parameterized fixture models arbitrary external callback JSON
    callback_context: str | dict[str, Any],
    expected_source_type: TaintSourceType,
) -> None:
    """External callback data remains user-role input with explicit taint."""
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    processing_service = TaintedReplyService()
    chat_interface = WebChatInterface(db_engine, notifier=None, stream_hub=None)
    payload = _payload()
    payload.update({
        "callback_context": callback_context,
        "automation_id": "automation-42",
        "tool_call_review_trigger_type": trigger_type,
        "tool_call_review_trigger_definition": None,
        "tool_call_review_trigger_payload_present": True,
    })

    await handle_llm_callback(
        _exec_context(Database(engine=db_engine), processing_service, chat_interface),
        payload,
    )

    assert processing_service.last_trigger_role == "user"
    assert processing_service.last_initial_taint_sources is not None
    assert len(processing_service.last_initial_taint_sources) == 1
    source = processing_service.last_initial_taint_sources[0]
    assert source.source_type is expected_source_type
    assert source.source_id == "automation:automation-42"
    assert source.tier is SourceTrustTier.UNKNOWN_EXTERNAL

    trigger_rows = await Database(engine=db_engine).fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.role == "user")
    )
    matching_rows = [
        row for row in trigger_rows if "external" in (row["content"] or "")
    ]
    assert len(matching_rows) == 1
    assert matching_rows[0]["taint_metadata_version"] == "runtime_v1"
    assert matching_rows[0]["taint_metadata_json"] is not None
    assert matching_rows[0]["taint_metadata_json"]["max_tier"] == "unknown_external"

    privileged_rows = await Database(engine=db_engine).fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.role == "system")
    )
    assert not any("external" in (row["content"] or "") for row in privileged_rows)


@pytest.mark.asyncio
async def test_reminder_without_provenance_is_tainted_user_role(
    db_engine: AsyncEngine,
) -> None:
    """A reminder definition is not trusted merely because the system scheduled it."""
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    processing_service = TaintedReplyService()
    chat_interface = WebChatInterface(db_engine, notifier=None, stream_hub=None)
    payload = _payload()
    payload.update({
        "callback_context": "Send the unattended reminder",
        "reminder_config": {
            "is_reminder": True,
            "follow_up": False,
            "follow_up_interval": "30 minutes",
            "max_follow_ups": 0,
            "current_attempt": 1,
        },
        "tool_call_review_trigger_type": "reminder",
        "tool_call_review_trigger_definition": "Send the unattended reminder",
        "tool_call_review_trigger_payload_present": False,
    })

    await handle_llm_callback(
        _exec_context(Database(engine=db_engine), processing_service, chat_interface),
        payload,
    )

    assert processing_service.last_review_trigger is not None
    assert processing_service.last_review_trigger.definition_taint_metadata is None
    assert processing_service.last_trigger_role == "user"
    assert processing_service.last_initial_taint_sources is not None
    assert len(processing_service.last_initial_taint_sources) == 1
    source = processing_service.last_initial_taint_sources[0]
    assert source.source_type is TaintSourceType.AUTOMATION_TRIGGER
    assert source.tier is SourceTrustTier.UNKNOWN_EXTERNAL


class _FailingDeliveryInterface(WebChatInterface):
    """A web interface whose send fails once, then succeeds."""

    def __init__(self, db_engine: AsyncEngine) -> None:
        super().__init__(db_engine, notifier=None, stream_hub=None)
        self.send_attempts = 0

    async def send_message(self, *args: object, **kwargs: object) -> str:
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise RuntimeError("transient delivery failure")
        return await super().send_message(*args, **kwargs)  # type: ignore[arg-type] # passthrough of the interface signature


class _NoDeliveryIdInterface(WebChatInterface):
    """A web interface that reports its first send as undelivered."""

    def __init__(self, db_engine: AsyncEngine) -> None:
        super().__init__(db_engine, notifier=None, stream_hub=None)
        self.send_attempts = 0

    async def send_message(self, *args: object, **kwargs: object) -> str:
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise ChatDeliveryError("no delivery id", transient=True)
        return await super().send_message(*args, **kwargs)  # type: ignore[arg-type] # passthrough of the interface signature


@pytest.mark.asyncio
async def test_callback_treats_a_failed_delivery_as_a_failed_send(
    db_engine: AsyncEngine,
) -> None:
    """A ChatDeliveryError is how the interface reports a failed delivery.

    Continuing past it would let the task complete with nothing sent and no
    retry, silently dropping the reply the turn already generated.
    """
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    processing_service = TaintedReplyService()
    chat_interface = _NoDeliveryIdInterface(db_engine)

    with pytest.raises(RuntimeError, match="Failed to send LLM callback response"):
        await handle_llm_callback(
            _exec_context(
                Database(engine=db_engine), processing_service, chat_interface
            ),
            _payload(),
        )

    # The retry resumes at delivery rather than regenerating.
    await handle_llm_callback(
        _exec_context(Database(engine=db_engine), processing_service, chat_interface),
        _payload(),
    )

    assert processing_service.call_count == 1
    assert chat_interface.send_attempts == 2

    user_rows = await Database(engine=db_engine).fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.turn_id == "worker_turn")
        .where(message_history_table.c.role == "user")
    )
    assert len(user_rows) == 1
    trigger_row = user_rows[0]
    assert trigger_row["is_internal"] is True
    assert trigger_row["processing_profile_id"] == "callback_profile"
    assert trigger_row["taint_metadata_version"] == "runtime_v1"
    assert trigger_row["taint_metadata_json"] is not None
    assert trigger_row["taint_metadata_json"]["max_tier"] == "unknown_external"


@pytest.mark.asyncio
async def test_callback_retry_resumes_at_delivery_without_rerunning_the_turn(
    db_engine: AsyncEngine,
) -> None:
    """A retry after a failed send must not run the LLM turn a second time.

    The turn's messages and its tools' writes are durable as soon as they
    happen, so regenerating would repeat every stateful tool the turn used.
    Every attempt of a task shares a turn id, and an assistant reply with no
    interface_message_id means "generated but never delivered" -- so the retry
    resumes at delivery.
    """
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    processing_service = TaintedReplyService()
    chat_interface = _FailingDeliveryInterface(db_engine)

    # First attempt: generation succeeds, delivery fails.
    with pytest.raises(RuntimeError, match="Failed to send LLM callback response"):
        await handle_llm_callback(
            _exec_context(
                Database(engine=db_engine), processing_service, chat_interface
            ),
            _payload(),
        )
    assert processing_service.call_count == 1

    # The retry reuses the same turn id, as the task worker gives it.
    await handle_llm_callback(
        _exec_context(Database(engine=db_engine), processing_service, chat_interface),
        _payload(),
    )

    # The LLM turn ran exactly once across both attempts.
    assert processing_service.call_count == 1
    assert chat_interface.send_attempts == 2
