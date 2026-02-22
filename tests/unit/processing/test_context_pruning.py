"""Tests for context length pruning."""

import pytest

from family_assistant.llm import ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction
from family_assistant.processing import prune_messages_for_context


def _make_tool_call(call_id: str, name: str = "search") -> ToolCallItem:
    """Create a ToolCallItem for tests."""
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments='{"q":"test"}'),
    )


@pytest.mark.no_db
class TestPruneMessagesForContext:
    def test_prunes_old_tool_results(self) -> None:
        """Tool results from older turns get truncated."""
        messages: list[LLMMessage] = [
            SystemMessage(content="System prompt"),
            UserMessage(content="First question"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc1")],
            ),
            ToolMessage(tool_call_id="tc1", content="A" * 10000, name="search"),
            AssistantMessage(content="Here's the result"),
            UserMessage(content="Second question"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc2")],
            ),
            ToolMessage(tool_call_id="tc2", content="B" * 5000, name="search"),
            AssistantMessage(content="Latest result"),
        ]

        pruned = prune_messages_for_context(messages)

        # Old tool result (tc1) should be truncated
        tool_msg_1 = [
            m for m in pruned if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
        ][0]
        assert len(tool_msg_1.content) < 200
        assert "truncated" in tool_msg_1.content.lower()
        assert "10000" in tool_msg_1.content

        # Latest tool result (tc2) should be preserved
        tool_msg_2 = [
            m for m in pruned if isinstance(m, ToolMessage) and m.tool_call_id == "tc2"
        ][0]
        assert tool_msg_2.content == "B" * 5000

    def test_preserves_system_prompt(self) -> None:
        """System prompt is never pruned."""
        messages: list[LLMMessage] = [
            SystemMessage(content="Important system prompt"),
            UserMessage(content="Question 1"),
            AssistantMessage(content="Answer 1"),
            UserMessage(content="Question 2"),
            AssistantMessage(content="Answer 2"),
        ]

        pruned = prune_messages_for_context(messages)
        assert pruned[0].role == "system"
        assert isinstance(pruned[0], SystemMessage)
        assert pruned[0].content == "Important system prompt"

    def test_drops_oldest_turns(self) -> None:
        """When messages are very long, oldest turns get dropped."""
        messages: list[LLMMessage] = [SystemMessage(content="System")]
        for i in range(10):
            messages.append(UserMessage(content=f"Question {i} " + "x" * 5000))
            messages.append(AssistantMessage(content=f"Answer {i} " + "y" * 5000))

        pruned = prune_messages_for_context(messages)

        # System prompt preserved
        assert pruned[0].role == "system"
        # Most recent turns preserved
        assert any(
            "Question 9" in str(m.content) for m in pruned if isinstance(m, UserMessage)
        )
        # Oldest turns dropped
        assert not any(
            "Question 0" in str(m.content) for m in pruned if isinstance(m, UserMessage)
        )

    def test_no_pruning_needed_returns_same(self) -> None:
        """Short conversations are returned as-is."""
        messages: list[LLMMessage] = [
            SystemMessage(content="System"),
            UserMessage(content="Hi"),
            AssistantMessage(content="Hello!"),
        ]

        pruned = prune_messages_for_context(messages)
        assert len(pruned) == len(messages)

    def test_does_not_modify_input_list(self) -> None:
        """The input list should not be modified."""
        messages: list[LLMMessage] = [
            SystemMessage(content="System prompt"),
            UserMessage(content="Question"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc1")],
            ),
            ToolMessage(tool_call_id="tc1", content="A" * 10000, name="search"),
            AssistantMessage(content="Result"),
            UserMessage(content="Follow-up"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc2")],
            ),
            ToolMessage(tool_call_id="tc2", content="B" * 100, name="search"),
            AssistantMessage(content="Latest"),
        ]
        original_len = len(messages)
        tool_msg = messages[3]
        assert isinstance(tool_msg, ToolMessage)
        original_content = tool_msg.content

        prune_messages_for_context(messages)

        assert len(messages) == original_len
        assert isinstance(messages[3], ToolMessage)
        assert messages[3].content == original_content

    def test_preserves_tool_message_metadata(self) -> None:
        """Pruned tool messages retain name, tool_call_id, and error_traceback."""
        messages: list[LLMMessage] = [
            SystemMessage(content="System"),
            UserMessage(content="Q1"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc1", "my_tool")],
            ),
            ToolMessage(
                tool_call_id="tc1",
                content="A" * 10000,
                name="my_tool",
                error_traceback="some traceback",
            ),
            AssistantMessage(content="Done"),
            UserMessage(content="Q2"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc2")],
            ),
            ToolMessage(tool_call_id="tc2", content="B" * 5000, name="search"),
            AssistantMessage(content="Latest"),
        ]

        pruned = prune_messages_for_context(messages)

        tool_msg_1 = [
            m for m in pruned if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
        ][0]
        assert tool_msg_1.name == "my_tool"
        assert tool_msg_1.tool_call_id == "tc1"
        assert tool_msg_1.error_traceback == "some traceback"

    def test_keeps_at_least_3_turns(self) -> None:
        """Even with many turns, at least 3 are kept."""
        messages: list[LLMMessage] = [SystemMessage(content="System")]
        for i in range(20):
            messages.append(UserMessage(content=f"Question {i} " + "x" * 5000))
            messages.append(AssistantMessage(content=f"Answer {i} " + "y" * 5000))

        pruned = prune_messages_for_context(messages)

        user_messages = [m for m in pruned if isinstance(m, UserMessage)]
        assert len(user_messages) >= 3
        # Most recent 3 turns should be present
        for i in range(17, 20):
            assert any(f"Question {i}" in str(m.content) for m in user_messages)

    def test_trailing_user_message_preserves_recent_tool_results(self) -> None:
        """When the last message is a UserMessage, recent tool results are still preserved."""
        messages: list[LLMMessage] = [
            SystemMessage(content="System prompt"),
            UserMessage(content="First question"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc1")],
            ),
            ToolMessage(tool_call_id="tc1", content="A" * 10000, name="search"),
            AssistantMessage(content="Here's the result"),
            UserMessage(content="Second question"),
            AssistantMessage(
                content=None,
                tool_calls=[_make_tool_call("tc2")],
            ),
            ToolMessage(tool_call_id="tc2", content="B" * 5000, name="search"),
            AssistantMessage(content="Latest result"),
            # Trailing UserMessage — e.g. retrying with a new prompt
            UserMessage(content="Follow-up question"),
        ]

        pruned = prune_messages_for_context(messages)

        # Old tool result (tc1) should be truncated
        tool_msg_1 = [
            m for m in pruned if isinstance(m, ToolMessage) and m.tool_call_id == "tc1"
        ][0]
        assert len(tool_msg_1.content) < 200
        assert "truncated" in tool_msg_1.content.lower()

        # Most recent tool result (tc2) should be preserved even though
        # the message list ends with a UserMessage
        tool_msg_2 = [
            m for m in pruned if isinstance(m, ToolMessage) and m.tool_call_id == "tc2"
        ][0]
        assert tool_msg_2.content == "B" * 5000

    def test_no_tool_messages_still_works(self) -> None:
        """Conversations without tool messages still prune by dropping old turns."""
        messages: list[LLMMessage] = [SystemMessage(content="System")]
        for i in range(10):
            messages.append(UserMessage(content=f"Question {i} " + "x" * 5000))
            messages.append(AssistantMessage(content=f"Answer {i} " + "y" * 5000))

        pruned = prune_messages_for_context(messages)

        assert pruned[0].role == "system"
        user_messages = [m for m in pruned if isinstance(m, UserMessage)]
        assert len(user_messages) >= 3
