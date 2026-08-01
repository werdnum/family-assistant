"""Chat Completions payloads must not carry our internal bookkeeping.

`provider_metadata` holds another provider's reasoning state and is not part of
the Chat Completions schema. Real OpenAI accepts the unknown field and answers
normally, so nothing fails there -- which is exactly why this needs a test. The
breakage is on OpenAI-*compatible* endpoints that validate strictly and reject
the request, and the payload is useless to the recipient either way.
"""

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    ImageUrlContentPart,
    LLMMessage,
    UserMessage,
)
from family_assistant.llm.providers.openai_client import OpenAIClient

ANTHROPIC_METADATA: dict[str, object] = {
    "provider": "anthropic",
    "thinking_blocks": [
        {
            "type": "thinking",
            "thinking": "Private reasoning that belongs to the other provider.",
            "signature": "ErUBCkYIBRgCKkBm2n0p",
        }
    ],
}


@pytest.fixture
def client() -> OpenAIClient:
    return OpenAIClient(api_key="test-key", model="gpt-4.1-nano")


def test_provider_metadata_is_stripped_from_chat_messages(
    client: OpenAIClient,
) -> None:
    """A mid-thread provider switch must not forward the previous provider's state."""
    message = AssistantMessage(content="Hello", provider_metadata=ANTHROPIC_METADATA)

    serialized = client._to_chat_completions_message(message)

    assert "provider_metadata" not in serialized
    assert serialized["content"] == "Hello"


def test_stripping_leaves_the_rest_of_the_message_intact(
    client: OpenAIClient,
) -> None:
    """Only the internal field goes; role, content and tool calls stay."""
    message = AssistantMessage(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_1",
                type="function",
                function=ToolCallFunction(
                    name="calculate", arguments='{"expression": "1+1"}'
                ),
            )
        ],
        provider_metadata=ANTHROPIC_METADATA,
    )

    serialized = client._to_chat_completions_message(message)

    assert "provider_metadata" not in serialized
    assert serialized["role"] == "assistant"
    tool_calls = serialized["tool_calls"]
    assert isinstance(tool_calls, list)
    assert tool_calls[0]["id"] == "call_1"


def test_messages_without_metadata_are_unchanged(client: OpenAIClient) -> None:
    """The common case must not be disturbed by the stripping."""
    message: LLMMessage = UserMessage(content="What is 2+2?")

    serialized = client._to_chat_completions_message(message)

    assert serialized["role"] == "user"
    assert serialized["content"] == "What is 2+2?"


def test_attachment_id_is_stripped_from_image_parts(client: OpenAIClient) -> None:
    """`attachment_id` is ours, and Chat Completions has no field for it.

    It exists so the Responses path can name an attachment the model cannot
    read. Chat Completions offers no such handoff, and a strictly-validating
    compatible endpoint rejects the whole multimodal request over the unknown
    key -- taking the image down with it.
    """
    message: LLMMessage = UserMessage(
        content=[
            ImageUrlContentPart(
                type="image_url",
                image_url={"url": "data:image/png;base64,aGVsbG8="},
                attachment_id="att-1234",
            )
        ]
    )

    serialized = client._to_chat_completions_message(message)

    content = serialized["content"]
    assert isinstance(content, list)
    assert "attachment_id" not in content[0]
    assert content[0]["image_url"] == {"url": "data:image/png;base64,aGVsbG8="}
