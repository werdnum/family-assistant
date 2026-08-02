"""Tests for repairing tool calls whose result never made it into history.

A turn interrupted while a tool is still running leaves its assistant tool call
persisted with no matching result. Replaying that history is a hard error on
providers that validate the pairing, so the history preparation repairs it.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.service import ABANDONED_TOOL_CALL_RESULT
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition


class _NoToolsProvider:
    async def get_tool_definitions(self) -> "list[ToolDefinition]":
        return []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool arguments from external LLM tool call
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        raise AssertionError("No tool should be executed by these tests")

    async def close(self) -> None:
        pass


def _tool_call(call_id: str, name: str = "execute_shell") -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments='{"command":"true"}'),
    )


def _unanswered_call_ids(messages: list[LLMMessage]) -> set[str]:
    """Call ids in the list that no tool message answers."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return {
        tool_call.id
        for message in messages
        if isinstance(message, AssistantMessage) and message.tool_calls
        for tool_call in message.tool_calls
    } - answered


@pytest.mark.no_db
class TestRepairUnmatchedToolCalls:
    def test_abandoned_call_gets_a_placeholder_result(self) -> None:
        """A call whose result was never written is answered, not dropped."""
        messages: list[LLMMessage] = [
            UserMessage(content="Analyse these scans"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            UserMessage(content="Actually, it was a dental x-ray"),
        ]

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (1, 0)
        assert messages == [
            UserMessage(content="Analyse these scans"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(
                tool_call_id="call_a",
                name="execute_shell",
                content=ABANDONED_TOOL_CALL_RESULT,
            ),
            UserMessage(content="Actually, it was a dental x-ray"),
        ]

    def test_placeholder_is_inserted_directly_after_its_call(self) -> None:
        """The synthesized result pairs with the call it belongs to."""
        messages: list[LLMMessage] = [
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="done"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_b")]),
            UserMessage(content="stop"),
        ]

        ProcessingService._repair_unmatched_tool_calls(messages)

        assert [type(message) for message in messages] == [
            AssistantMessage,
            ToolMessage,
            AssistantMessage,
            ToolMessage,
            UserMessage,
        ]
        placeholder = messages[3]
        assert isinstance(placeholder, ToolMessage)
        assert placeholder.tool_call_id == "call_b"

    def test_answered_calls_are_left_alone(self) -> None:
        """A well-formed history is returned unchanged."""
        messages: list[LLMMessage] = [
            SystemMessage(content="System"),
            UserMessage(content="Question"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="ok"),
            AssistantMessage(content="Answer"),
        ]
        original = list(messages)

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (0, 0)
        assert messages == original

    def test_only_the_unanswered_call_of_a_parallel_batch_is_filled_in(self) -> None:
        """One assistant message can hold several calls with mixed outcomes."""
        messages: list[LLMMessage] = [
            AssistantMessage(
                content=None,
                tool_calls=[_tool_call("call_a"), _tool_call("call_b")],
            ),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="ok"),
        ]

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (1, 0)
        answers = {
            message.tool_call_id: message.content
            for message in messages
            if isinstance(message, ToolMessage)
        }
        assert answers == {"call_a": "ok", "call_b": ABANDONED_TOOL_CALL_RESULT}

    def test_result_without_its_call_is_dropped(self) -> None:
        """A tool result whose call is absent cannot be represented at all."""
        messages: list[LLMMessage] = [
            UserMessage(content="Question"),
            ToolMessage(tool_call_id="call_gone", name="execute_shell", content="ok"),
            AssistantMessage(content="Answer"),
        ]

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (0, 1)
        assert messages == [
            UserMessage(content="Question"),
            AssistantMessage(content="Answer"),
        ]

    def test_result_preceding_its_own_call_is_replaced(self) -> None:
        """Order matters: a result must follow the call it answers.

        The out-of-order result is unusable, which leaves its call needing a
        placeholder like any other unanswered call.
        """
        messages: list[LLMMessage] = [
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="ok"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
        ]

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (1, 1)
        assert messages == [
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(
                tool_call_id="call_a",
                name="execute_shell",
                content=ABANDONED_TOOL_CALL_RESULT,
            ),
        ]

    def test_late_result_is_moved_back_to_its_call(self) -> None:
        """A result separated from its call by a rival prompt is reattached.

        This is the incident's own row order: the slow tool is still running
        when the rival turn persists its prompt, so the result lands after it.
        Providers that want a call's results attached to the calling turn reject
        that, and the real output is worth keeping — so it moves up to the call
        rather than being dropped for a placeholder.
        """
        messages: list[LLMMessage] = [
            UserMessage(content="OCR these scans"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            UserMessage(content="Actually, it was a dental x-ray"),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="page 1…"),
        ]

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (0, 0)
        assert messages == [
            UserMessage(content="OCR these scans"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="page 1…"),
            UserMessage(content="Actually, it was a dental x-ray"),
        ]
        assert not _unanswered_call_ids(messages)

    def test_duplicate_result_for_one_call_is_dropped(self) -> None:
        """A call is answered exactly once; a second result cannot be placed."""
        messages: list[LLMMessage] = [
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="first"),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="second"),
        ]

        synthesized, dropped = ProcessingService._repair_unmatched_tool_calls(messages)

        assert (synthesized, dropped) == (0, 1)
        assert messages == [
            AssistantMessage(content=None, tool_calls=[_tool_call("call_a")]),
            ToolMessage(tool_call_id="call_a", name="execute_shell", content="first"),
        ]

    def test_empty_history_is_a_no_op(self) -> None:
        messages: list[LLMMessage] = []

        assert ProcessingService._repair_unmatched_tool_calls(messages) == (0, 0)
        assert messages == []


async def test_interrupted_tool_call_in_stored_history_is_repaired(
    db_engine: "AsyncEngine",
) -> None:
    """The turn after an interruption sends the LLM a representable history.

    Reproduces the production failure: a turn persists its assistant tool call,
    the tool is still running when a second turn starts on the conversation, and
    the replayed history contains a call with no result.
    """
    seen_messages: list[LLMMessage] = []

    def capture(args: MatcherArgs) -> LLMOutput:
        seen_messages.extend(args["messages"])
        return LLMOutput(content="Understood.", tool_calls=None)

    service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(rules=[(lambda _args: True, capture)]),
        tools_provider=_NoToolsProvider(),
        service_config=ProcessingServiceConfig(
            prompts={},
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="interrupted_turn_profile",
        ),
        context_providers=[],
        server_url="http://test.com",
        app_config=AppConfig(),
    )

    conversation_id = "conv-interrupted-tool-call"
    interrupted_turn_at = service.clock.now() - timedelta(minutes=2)
    async with DatabaseContext(engine=db_engine) as db_context:
        stored: list[LLMMessage] = [
            UserMessage(content="OCR these medical records"),
            AssistantMessage(content=None, tool_calls=[_tool_call("call_running")]),
        ]
        for index, message in enumerate(stored):
            await db_context.message_history.add_message(
                message,
                interface_type="web",
                conversation_id=conversation_id,
                timestamp=interrupted_turn_at + timedelta(seconds=index),
                turn_id="turn-interrupted",
                processing_profile_id="interrupted_turn_profile",
            )

        result = await service.handle_chat_interaction(
            db_context=db_context,
            interface_type="web",
            conversation_id=conversation_id,
            trigger_content_parts=[
                {"type": "text", "text": "Ah, it was a dental x-ray finding"}
            ],
            trigger_interface_message_id=None,
            user_name="Test User",
        )

    assert result.text_reply == "Understood."
    assert _unanswered_call_ids(seen_messages) == set()
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "call_running"
        and message.content == ABANDONED_TOOL_CALL_RESULT
        for message in seen_messages
    )
