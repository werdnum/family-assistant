"""Functional tests: a woken turn can end without messaging the user.

Automation, event and scheduled wakes are offered ``end_turn_quietly``. Ending
with it records the turn in history as an internal row and delivers nothing.
Turns somebody is owed a reply on -- a user's own message, the first firing of
a reminder, a script failure notice -- are never offered it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.content_parts import text_content
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.quiet_turn import END_TURN_QUIETLY_TOOL_NAME
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import (
    LlmCallbackPayload,
    NonRetryableTaskError,
    handle_llm_callback,
)
from family_assistant.tools import LocalToolsProvider
from family_assistant.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage

TEST_INTERFACE_TYPE = "test"
TEST_CONVERSATION_ID = "quiet_turn_chat"
TEST_USER_NAME = "QuietTester"

_ECHO_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo a value back.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}


class _ScriptedLLMClient(RuleBasedMockLLMClient):
    """Returns a queued output per call and records the tools each call offered."""

    def __init__(self, outputs: Sequence[LLMOutput]) -> None:
        super().__init__(rules=[], default_response=LLMOutput(content="unused"))
        self._outputs = list(outputs)
        self.offered_tool_names: list[set[str]] = []

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        del messages, tool_choice
        self.offered_tool_names.append({
            tool["function"]["name"] for tool in tools or []
        })
        return self._outputs.pop(0) if self._outputs else LLMOutput(content="done")


async def _echo(value: str, **_kwargs: Any) -> ToolResult:  # noqa: ANN401
    return ToolResult(text=value)


def _tool_call(call_id: str, name: str, arguments: str) -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments=arguments),
    )


def _quiet_call(reason: str = "Washer still running") -> ToolCallItem:
    return _tool_call(
        "call_quiet", END_TURN_QUIETLY_TOOL_NAME, f'{{"reason": "{reason}"}}'
    )


def _make_service(
    client: RuleBasedMockLLMClient, *, max_iterations: int = 5
) -> ProcessingService:
    return ProcessingService(
        llm_client=client,
        tools_provider=LocalToolsProvider(
            definitions=[_ECHO_TOOL], implementations={"echo": _echo}
        ),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            id="test_profile",
            max_iterations=max_iterations,
        ),
        context_providers=[],
        server_url="http://localhost:8000",
        app_config=AppConfig(),
    )


def _exec_context(
    db_context: Database,
    processing_service: object,
    chat_interface: ChatInterface,
    *,
    turn_id: str = "quiet_callback_turn",
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CONVERSATION_ID,
        user_name=TEST_USER_NAME,
        turn_id=turn_id,
        db_context=db_context,
        processing_service=cast("Any", processing_service),
        clock=SystemClock(),
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
        credential_resolvers=None,
        api_backend=None,
        task_priority=TaskPriority.INTERACTIVE,
    )


def _payload(
    *,
    reminder_attempt: int | None = None,
    reminder_follow_up: bool = False,
    trigger_type: str | None = None,
) -> LlmCallbackPayload:
    payload: LlmCallbackPayload = {
        "interface_type": TEST_INTERFACE_TYPE,
        "conversation_id": TEST_CONVERSATION_ID,
        "user_name": TEST_USER_NAME,
        "callback_context": "Check whether the washer has finished.",
        "scheduling_timestamp": datetime.now(UTC).isoformat(),
        "created_by_user_id": "quiet-owner",
    }
    if reminder_attempt is not None:
        payload["reminder_config"] = {
            "is_reminder": True,
            "follow_up": reminder_follow_up,
            "current_attempt": reminder_attempt,
        }
    if trigger_type is not None:
        payload["tool_call_review_trigger_type"] = trigger_type
    return payload


def _chat_interface() -> AsyncMock:
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "sent_message_id"
    return chat_interface


@pytest.mark.asyncio
async def test_a_woken_turn_can_end_without_messaging_the_user(
    db_engine: AsyncEngine,
) -> None:
    """The tools still run; the closing row is internal and nothing is sent."""
    client = _ScriptedLLMClient([
        LLMOutput(tool_calls=[_tool_call("call_echo", "echo", '{"value": "ok"}')]),
        LLMOutput(tool_calls=[_quiet_call()]),
    ])
    chat_interface = _chat_interface()
    db_context = Database(engine=db_engine)

    await handle_llm_callback(
        _exec_context(db_context, _make_service(client), chat_interface),
        _payload(),
    )

    assert END_TURN_QUIETLY_TOOL_NAME in client.offered_tool_names[0]
    chat_interface.send_message.assert_not_called()

    rows = await db_context.message_history.get_recent_with_metadata(
        interface_type=TEST_INTERFACE_TYPE, conversation_id=TEST_CONVERSATION_ID
    )
    tool_rows = [row for row in rows if row["role"] == "tool"]
    assert [row["tool_name"] for row in tool_rows] == [
        "echo",
        END_TURN_QUIETLY_TOOL_NAME,
    ]
    closing_row = rows[-1]
    assert closing_row["role"] == "assistant"
    assert closing_row["is_internal"] is True
    assert "Washer still running" in (closing_row["content"] or "")
    assert closing_row["interface_message_id"] is None


@pytest.mark.asyncio
async def test_a_retry_after_a_quiet_end_does_not_rerun_the_turn(
    db_engine: AsyncEngine,
) -> None:
    """The quiet row closes the delivery checkpoint, so a retry stops there."""
    client = _ScriptedLLMClient([LLMOutput(tool_calls=[_quiet_call()])])
    chat_interface = _chat_interface()
    db_context = Database(engine=db_engine)
    service = _make_service(client)

    for _ in range(2):
        await handle_llm_callback(
            _exec_context(db_context, service, chat_interface), _payload()
        )

    assert len(client.offered_tool_names) == 1
    chat_interface.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_a_user_turn_is_never_offered_a_quiet_end(
    db_engine: AsyncEngine,
) -> None:
    """A person who wrote is owed a reply: a quiet call is just an unknown tool."""
    client = _ScriptedLLMClient([
        LLMOutput(tool_calls=[_quiet_call()]),
        LLMOutput(content="Here is your answer."),
    ])
    db_context = Database(engine=db_engine)

    result = await _make_service(client).handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id="user_turn_chat",
        trigger_content_parts=[text_content("Is the washer done?")],
        trigger_interface_message_id="msg-1",
        user_name=TEST_USER_NAME,
    )

    assert all(
        END_TURN_QUIETLY_TOOL_NAME not in offered
        for offered in client.offered_tool_names
    )
    assert result.ended_quietly is False
    assert result.text_reply == "Here is your answer."


@pytest.mark.asyncio
async def test_an_empty_callback_reply_fails_without_a_retry(
    db_engine: AsyncEngine,
) -> None:
    """Saying nothing without the tool is a fault, and retrying would rerun tools."""
    client = _ScriptedLLMClient([
        LLMOutput(tool_calls=[_tool_call("call_echo", "echo", '{"value": "ok"}')]),
        LLMOutput(content=""),
        LLMOutput(content=""),
        LLMOutput(content=""),
    ])
    chat_interface = _chat_interface()

    with pytest.raises(NonRetryableTaskError):
        await handle_llm_callback(
            _exec_context(
                Database(engine=db_engine), _make_service(client), chat_interface
            ),
            _payload(),
        )
    chat_interface.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_a_quiet_end_stays_available_when_the_tool_budget_runs_out(
    db_engine: AsyncEngine,
) -> None:
    """The last tool result may be what shows there is nothing to report."""
    client = _ScriptedLLMClient([
        LLMOutput(tool_calls=[_tool_call("call_echo", "echo", '{"value": "ok"}')]),
        LLMOutput(tool_calls=[_quiet_call()]),
    ])
    chat_interface = _chat_interface()
    db_context = Database(engine=db_engine)

    await handle_llm_callback(
        _exec_context(
            db_context, _make_service(client, max_iterations=2), chat_interface
        ),
        _payload(),
    )

    assert client.offered_tool_names[-1] == {END_TURN_QUIETLY_TOOL_NAME}
    chat_interface.send_message.assert_not_called()
    rows = await db_context.message_history.get_recent_with_metadata(
        interface_type=TEST_INTERFACE_TYPE, conversation_id=TEST_CONVERSATION_ID
    )
    assert rows[-1]["is_internal"] is True


@pytest.mark.asyncio
async def test_an_empty_reminder_does_not_schedule_a_follow_up(
    db_engine: AsyncEngine,
) -> None:
    """A follow-up would claim a reminder was sent that never was."""
    client = _ScriptedLLMClient([LLMOutput(content="")] * 3)
    db_context = Database(engine=db_engine)

    with pytest.raises(NonRetryableTaskError):
        await handle_llm_callback(
            _exec_context(db_context, _make_service(client), _chat_interface()),
            _payload(reminder_attempt=1, reminder_follow_up=True),
        )

    assert await db_context.tasks.get_all(task_type="llm_callback") == []


class _OfferCapturingService:
    """Fake processing service recording whether a quiet end was offered."""

    def __init__(self) -> None:
        self.service_config = SimpleNamespace(
            id="callback_profile", allow_wake_llm=True
        )
        self.processing_services_registry: dict[str, object] = {}
        self.allow_quiet_end: object = "unset"

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        self.allow_quiet_end = kwargs.get("allow_quiet_end")
        return ChatInteractionResult.success(text_reply="reply")


@pytest.mark.parametrize(
    ("payload", "offered"),
    [
        pytest.param(_payload(), True, id="scheduled-callback"),
        pytest.param(_payload(trigger_type="event_listener"), True, id="event"),
        pytest.param(_payload(reminder_attempt=2), True, id="reminder-follow-up"),
        pytest.param(_payload(reminder_attempt=1), False, id="reminder-first-firing"),
        pytest.param(
            _payload(trigger_type="script_failure"), False, id="script-failure"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_quiet_end_is_offered_only_where_nobody_is_owed_a_reply(
    db_engine: AsyncEngine,
    payload: LlmCallbackPayload,
    offered: bool,
) -> None:
    service = _OfferCapturingService()

    await handle_llm_callback(
        _exec_context(Database(engine=db_engine), service, _chat_interface()),
        payload,
    )

    assert service.allow_quiet_end is offered
