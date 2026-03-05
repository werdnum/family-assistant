"""Tests for message validation rules."""

import pytest
from pydantic import ValidationError

from family_assistant.llm.messages import AssistantMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.repositories.message_history import (
    MessageHistoryRepository,
)


def _make_tool_call() -> list[ToolCallItem]:
    return [
        ToolCallItem(
            id="call_1",
            type="function",
            function=ToolCallFunction(name="test", arguments="{}"),
        )
    ]


class TestAssistantMessageValidation:
    def test_rejects_none_content_without_tool_calls(self) -> None:
        with pytest.raises(ValidationError, match="must have content or tool_calls"):
            AssistantMessage(content=None, tool_calls=None)

    def test_rejects_empty_content_without_tool_calls(self) -> None:
        with pytest.raises(ValidationError, match="must have content or tool_calls"):
            AssistantMessage(content="", tool_calls=None)

    def test_rejects_whitespace_content_without_tool_calls(self) -> None:
        with pytest.raises(ValidationError, match="must have content or tool_calls"):
            AssistantMessage(content="   ", tool_calls=None)

    def test_accepts_content_with_text(self) -> None:
        msg = AssistantMessage(content="Hello")
        assert msg.content == "Hello"

    def test_accepts_none_content_with_tool_calls(self) -> None:
        msg = AssistantMessage(content=None, tool_calls=_make_tool_call())
        assert msg.tool_calls is not None

    def test_accepts_empty_content_with_tool_calls(self) -> None:
        msg = AssistantMessage(content="", tool_calls=_make_tool_call())
        assert msg.tool_calls is not None

    def test_accepts_content_and_tool_calls(self) -> None:
        msg = AssistantMessage(content="Calling tool", tool_calls=_make_tool_call())
        assert msg.content == "Calling tool"
        assert msg.tool_calls is not None


class TestRepositoryMessageValidation:
    """Test that MessageHistoryRepository._validate_message catches invalid messages."""

    def test_rejects_assistant_with_empty_content_no_tool_calls(self) -> None:
        with pytest.raises(ValidationError, match="must have content or tool_calls"):
            MessageHistoryRepository._validate_message(
                role="assistant", content="", tool_calls=None
            )

    def test_rejects_assistant_with_none_content_no_tool_calls(self) -> None:
        with pytest.raises(ValidationError, match="must have content or tool_calls"):
            MessageHistoryRepository._validate_message(
                role="assistant", content=None, tool_calls=None
            )

    def test_accepts_assistant_with_content(self) -> None:
        MessageHistoryRepository._validate_message(
            role="assistant", content="Hello", tool_calls=None
        )

    def test_accepts_assistant_with_tool_calls(self) -> None:
        MessageHistoryRepository._validate_message(
            role="assistant", content=None, tool_calls=_make_tool_call()
        )

    def test_accepts_user_message(self) -> None:
        MessageHistoryRepository._validate_message(role="user", content="Hello")

    def test_accepts_tool_message(self) -> None:
        MessageHistoryRepository._validate_message(
            role="tool",
            content="result",
            tool_call_id="call_1",
            tool_name="test_tool",
        )

    def test_accepts_error_message(self) -> None:
        MessageHistoryRepository._validate_message(
            role="error", content="Something failed"
        )

    def test_rejects_unknown_role(self) -> None:
        with pytest.raises(ValueError, match="Unknown message role"):
            MessageHistoryRepository._validate_message(role="unknown", content="test")
