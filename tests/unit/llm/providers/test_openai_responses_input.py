"""Regression tests for OpenAI Responses API input conversion."""

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
