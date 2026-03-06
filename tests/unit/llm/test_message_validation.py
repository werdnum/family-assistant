"""Tests for message validation rules."""

import pytest
from pydantic import ValidationError

from family_assistant.llm.messages import AssistantMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem


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
