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


def _reasoning_message(encrypted_content: str | None) -> AssistantMessage:
    """An assistant turn whose stored output holds a reasoning item."""
    return AssistantMessage(
        content="",
        tool_calls=[],
        provider_metadata={
            "openai_response_output": [
                {
                    "type": "reasoning",
                    "id": "rs_123",
                    "summary": [],
                    "encrypted_content": encrypted_content,
                },
                {
                    "type": "function_call",
                    "id": "fc_123",
                    "call_id": "call_123",
                    "name": "calculate",
                    "arguments": '{"expression":"42 * 17"}',
                },
            ]
        },
    )


def test_reasoning_without_encrypted_content_is_dropped_when_unstored() -> None:
    """History predating the encrypted_content include must not 400 the request.

    With store=false the server holds no copy, so an item carrying no
    encrypted_content cannot be resolved and would reject the whole request.
    """
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input(
        [_reasoning_message(None)], store=False
    )

    assert [item["type"] for item in input_items] == ["function_call"]


def test_reasoning_with_encrypted_content_is_replayed() -> None:
    """The normal case still carries reasoning state forward."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input(
        [_reasoning_message("gAAAAAB-encrypted")], store=False
    )

    assert [item["type"] for item in input_items] == ["reasoning", "function_call"]
    assert input_items[0]["encrypted_content"] == "gAAAAAB-encrypted"


def test_reasoning_without_encrypted_content_is_kept_when_stored() -> None:
    """With store=true the server resolves the item by id, so it stays."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input(
        [_reasoning_message(None)], store=True
    )

    assert [item["type"] for item in input_items] == ["reasoning", "function_call"]


def test_responses_api_requires_explicit_opt_in() -> None:
    """A reasoning-capable model name alone must not switch APIs."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    assert client._uses_responses_api() is False


def test_responses_api_opt_in_via_model_parameters() -> None:
    """`use_responses_api` in llm_parameters is what selects the Responses API."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"use_responses_api": True}},
    )

    assert client._uses_responses_api() is True


def test_control_params_never_reach_the_api() -> None:
    """`use_responses_api` steers the client; sending it would be rejected."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={
            "gpt-5.6-sol": {"use_responses_api": True, "temperature": 0.5}
        },
    )

    params = client._build_responses_params([], None, None, stream=False)

    assert "use_responses_api" not in params
    assert params["temperature"] == 0.5


def test_responses_api_not_used_for_openai_compatible_backends() -> None:
    """OpenRouter and friends implement Chat Completions, not Responses."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"use_responses_api": True}},
        base_url="https://openrouter.ai/api/v1",
    )

    assert client._uses_responses_api() is False
