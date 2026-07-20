"""Regression tests for OpenAI Responses API input conversion."""

import json

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, LLMMessage
from family_assistant.llm.providers.openai_client import OpenAIClient


def test_responses_continuation_omits_response_status() -> None:
    """Completed response metadata must not be replayed as request input."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    messages: list[LLMMessage] = [
        AssistantMessage(
            content="",
            tool_calls=[],
            provider_metadata={
                "openai_response_output": [
                    {
                        "type": "function_call",
                        "id": "fc_123",
                        "status": "completed",
                        "call_id": "call_123",
                        "name": "calculate",
                        "arguments": '{"expression":"42 * 17"}',
                    }
                ]
            },
        )
    ]

    input_items = client._messages_to_responses_input(messages)

    assert input_items == [
        {
            "type": "function_call",
            "id": "fc_123",
            "call_id": "call_123",
            "name": "calculate",
            "arguments": '{"expression":"42 * 17"}',
        }
    ]


def test_synthesized_responses_assistant_message_omits_status() -> None:
    """Fallback assistant messages must use input-compatible fields too."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input([
        AssistantMessage(content="Hello")
    ])

    assert "status" not in input_items[0]


def test_responses_serializes_structured_tool_call_arguments() -> None:
    """Tool calls from providers such as Gemini must be replayed as JSON strings."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    arguments: dict[str, object] = {
        "location": "Melbourne",
        "units": "metric",
    }
    messages: list[LLMMessage] = [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_123",
                    type="function",
                    function=ToolCallFunction(
                        name="get_weather",
                        arguments=arguments,
                    ),
                )
            ],
        )
    ]

    input_items = client._messages_to_responses_input(messages)

    serialized_arguments = input_items[0]["arguments"]
    assert isinstance(serialized_arguments, str)
    assert json.loads(serialized_arguments) == arguments


def test_responses_preserves_string_tool_call_arguments() -> None:
    """Tool calls already encoded for OpenAI must not be double-serialized."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    arguments = '{"location":"Melbourne","units":"metric"}'
    messages: list[LLMMessage] = [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_123",
                    type="function",
                    function=ToolCallFunction(
                        name="get_weather",
                        arguments=arguments,
                    ),
                )
            ],
        )
    ]

    input_items = client._messages_to_responses_input(messages)

    assert input_items[0]["arguments"] == arguments
