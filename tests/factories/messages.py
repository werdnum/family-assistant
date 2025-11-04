"""
Test factories for creating LLM message objects.

These factory functions make it easy to create message objects in tests
with sensible defaults while allowing customization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from family_assistant.llm.messages import ContentPart
    from family_assistant.tools.types import ToolAttachment, ToolResult


def create_user_message(
    content: str | list[ContentPart] = "Test user message",
    **kwargs: Any,  # noqa: ANN401
) -> UserMessage:
    """
    Create a UserMessage for testing.

    Args:
        content: Message content (text or list of content parts)
        **kwargs: Additional fields to override

    Returns:
        UserMessage instance
    """
    return UserMessage(content=content, **kwargs)


def create_assistant_message(
    content: str | None = "Test assistant response",
    tool_calls: list[ToolCallItem] | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> AssistantMessage:
    """
    Create an AssistantMessage for testing.

    Args:
        content: Message content (can be None if tool_calls provided)
        tool_calls: List of tool calls made by assistant
        **kwargs: Additional fields to override

    Returns:
        AssistantMessage instance
    """
    return AssistantMessage(content=content, tool_calls=tool_calls, **kwargs)


def create_tool_message(
    tool_call_id: str = "test-call-id",
    content: str = "Tool execution result",
    name: str = "test_tool",
    tool_result: ToolResult | None = None,
    attachments_list: list[ToolAttachment] | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> ToolMessage:
    """
    Create a ToolMessage for testing.

    Args:
        tool_call_id: ID of the tool call this responds to
        content: Tool result content
        name: Tool/function name
        tool_result: Original ToolResult object (optional)
        attachments_list: List of ToolAttachment objects (optional)
        **kwargs: Additional fields to override

    Returns:
        ToolMessage instance
    """
    return ToolMessage(
        tool_call_id=tool_call_id,
        content=content,
        name=name,
        tool_result=tool_result,
        _attachments=attachments_list,
        **kwargs,
    )


def create_system_message(
    content: str = "You are a helpful assistant.",
    **kwargs: Any,  # noqa: ANN401
) -> SystemMessage:
    """
    Create a SystemMessage for testing.

    Args:
        content: System prompt content
        **kwargs: Additional fields to override

    Returns:
        SystemMessage instance
    """
    return SystemMessage(content=content, **kwargs)


def create_error_message(
    content: str = "An error occurred",
    error_traceback: str | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> ErrorMessage:
    """
    Create an ErrorMessage for testing.

    Args:
        content: Error message content
        error_traceback: Optional stack trace
        **kwargs: Additional fields to override

    Returns:
        ErrorMessage instance
    """
    return ErrorMessage(
        content=content,
        error_traceback=error_traceback,
        **kwargs,
    )


def create_tool_call(
    call_id: str = "test-call-123",
    function_name: str = "test_function",
    arguments: str = '{"arg1": "value1"}',
) -> ToolCallItem:
    """
    Create a ToolCallItem for testing.

    Args:
        call_id: Unique ID for this tool call
        function_name: Name of the function to call
        arguments: JSON string of arguments

    Returns:
        ToolCallItem instance
    """
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=function_name, arguments=arguments),
    )
