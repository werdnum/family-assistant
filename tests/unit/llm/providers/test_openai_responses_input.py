"""Regression tests for OpenAI Responses API input conversion."""

import json

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    AssistantMessage,
    AttachmentContentPart,
    ImageUrlContentPart,
    LLMMessage,
    TextContentPart,
    UserMessage,
)
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


def test_synthesized_assistant_message_is_byte_stable_across_requests() -> None:
    """A fabricated per-request id would silently defeat prompt caching.

    The input prefix has to be identical between turns for OpenAI to reuse a
    cached prefix. Nothing fails loudly when it is not -- the bill just goes up
    and every cassette stops matching -- so it is pinned here.
    """
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    messages: list[LLMMessage] = [
        UserMessage(content="What is the weather?"),
        AssistantMessage(content="It is sunny."),
    ]

    first = client._messages_to_responses_input(messages)
    second = client._messages_to_responses_input(messages)

    assert first == second
    assert "id" not in first[1]


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


def _reasoning_message(
    encrypted_content: str | None,
    *,
    originating_response_stored: bool | None = None,
) -> AssistantMessage:
    """An assistant turn whose stored output holds a reasoning item."""
    provider_metadata: dict[str, object] = {
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
    }
    if originating_response_stored is not None:
        provider_metadata["openai_response_stored"] = originating_response_stored
    return AssistantMessage(
        content="",
        tool_calls=[],
        provider_metadata=provider_metadata,
    )


def test_reasoning_without_encrypted_content_is_dropped_when_unstored() -> None:
    """History predating the encrypted_content include must not 400 the request.

    With store=false the server holds no copy, so an item carrying no
    encrypted_content cannot be resolved and would reject the whole request.
    """
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input([_reasoning_message(None)])

    assert [item["type"] for item in input_items] == ["function_call"]


def test_reasoning_with_encrypted_content_is_replayed() -> None:
    """The normal case still carries reasoning state forward."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input([
        _reasoning_message("gAAAAAB-encrypted")
    ])

    assert [item["type"] for item in input_items] == ["reasoning", "function_call"]
    assert input_items[0]["encrypted_content"] == "gAAAAAB-encrypted"


def test_reasoning_without_encrypted_content_is_kept_when_origin_was_stored() -> None:
    """The server can resolve an item whose originating response was stored."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input([
        _reasoning_message(None, originating_response_stored=True)
    ])

    assert [item["type"] for item in input_items] == ["reasoning", "function_call"]


def test_current_store_setting_does_not_rescue_unstored_history() -> None:
    """Changing store later cannot make a historical reasoning ID resolvable."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"store": True}},
    )

    params = client._build_responses_params(
        [_reasoning_message(None)], None, None, stream=False
    )

    input_items = params["input"]
    assert isinstance(input_items, list)
    assert [item["type"] for item in input_items] == ["function_call"]


def test_responses_store_requires_a_boolean() -> None:
    """String values must not silently enable server-side response storage."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"store": "false"}},
    )

    with pytest.raises(InvalidRequestError, match="store configuration"):
        client._build_responses_params([], None, None, stream=False)


async def test_nonstreaming_store_error_keeps_invalid_request_type() -> None:
    """Request error mapping preserves the local configuration exception."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"use_responses_api": True, "store": "false"}},
    )

    with pytest.raises(InvalidRequestError, match="store configuration"):
        await client.generate_response([UserMessage(content="Hello")])


async def test_streaming_store_error_is_typed_invalid_request() -> None:
    """Streaming exposes the same configuration failure as invalid_request."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"use_responses_api": True, "store": "false"}},
    )

    events = [
        event
        async for event in client.generate_response_stream([
            UserMessage(content="Hello")
        ])
    ]

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].metadata is not None
    assert events[0].metadata.get("error_type") == "invalid_request"


@pytest.mark.parametrize(
    "model",
    ["gpt-5.6-sol", "gpt-5.5", "gpt-4.1", "some-future-openai-model"],
)
def test_responses_api_is_the_default_for_direct_openai(model: str) -> None:
    """Every direct OpenAI model gets Responses without being enrolled.

    A model that has to be listed somewhere is a model someone can forget,
    which is how the previous name-prefix and opt-in designs both lost
    reasoning propagation silently.
    """
    client = OpenAIClient(api_key="test-key", model=model)

    assert client._uses_responses_api() is True


def test_responses_api_can_be_pinned_off_per_model() -> None:
    """The escape hatch back to Chat Completions still works."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"use_responses_api": False}},
    )

    assert client._uses_responses_api() is False


def test_non_boolean_use_responses_api_is_rejected() -> None:
    """YAML `use_responses_api: "false"` must not read as true."""
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        model_parameters={"gpt-5.6-sol": {"use_responses_api": "false"}},
    )

    with pytest.raises(InvalidRequestError, match="expected a boolean"):
        client._uses_responses_api()


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


def test_environment_base_url_also_counts_as_a_compatible_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OPENAI_BASE_URL` redirects the SDK without passing through our kwargs.

    Trusting the constructor argument would leave `_is_direct_openai` true and
    send every model to an endpoint's `/responses` route, which a
    Chat-Completions-only backend does not implement.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")

    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    assert client._uses_responses_api() is False


def test_direct_openai_is_still_detected_without_a_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case must not be misclassified by the stricter check."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    assert client._uses_responses_api() is True


@pytest.mark.parametrize(
    "message,expected_type",
    [
        pytest.param("Rate limit reached for gpt-5.6-sol", "rate_limit", id="rate"),
        pytest.param("The model does not exist (404)", "model_not_found", id="404"),
        pytest.param("Something unfamiliar happened", "unknown", id="unclassifiable"),
    ],
)
async def test_responses_stream_failures_carry_typed_metadata(
    message: str, expected_type: str
) -> None:
    """A failed Responses turn must classify like a Chat Completions one.

    Without this metadata `_map_stream_error_to_exception` can only raise a bare
    RuntimeError, collapsing a rate limit and a bad request into one shape.
    """
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    events = [
        event
        async for event in client._emit_events_from_responses_dicts(
            [{"type": "response.failed", "response": {"error": {"message": message}}}],
            originating_response_stored=False,
        )
    ]

    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].error == message
    assert events[0].metadata is not None
    assert events[0].metadata.get("error_type") == expected_type
    assert events[0].metadata.get("provider") == "openai"
    assert events[0].metadata.get("model") == "gpt-5.6-sol"


def test_unconvertible_content_part_raises_instead_of_being_dropped() -> None:
    """Silently filtering a part would strip content the user supplied."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    messages: list[LLMMessage] = [
        UserMessage(
            content=[
                TextContentPart(type="text", text="Look at this"),
                AttachmentContentPart(type="attachment", attachment_id="att_1"),
            ]
        )
    ]

    with pytest.raises(InvalidRequestError, match="attachment"):
        client._messages_to_responses_input(messages)


def test_text_and_image_parts_still_convert() -> None:
    """The convertible part types are unaffected by the strictness."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    messages: list[LLMMessage] = [
        UserMessage(
            content=[
                TextContentPart(type="text", text="What is in this image?"),
                ImageUrlContentPart(
                    type="image_url", image_url={"url": "data:image/png;base64,AAAA"}
                ),
            ]
        )
    ]

    input_items = client._messages_to_responses_input(messages)

    assert input_items[0]["content"] == [
        {"type": "input_text", "text": "What is in this image?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]


def _sole_user_content_part(url: str) -> dict[str, object]:
    """Convert a single media part through a direct-OpenAI client."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-terra")
    items = client._messages_to_responses_input([
        UserMessage(
            content=[ImageUrlContentPart(type="image_url", image_url={"url": url})]
        )
    ])
    content = items[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    return content[0]


def test_image_data_uri_becomes_an_input_image() -> None:
    url = "data:image/png;base64,aGVsbG8="

    assert _sole_user_content_part(url) == {"type": "input_image", "image_url": url}


def test_pdf_data_uri_becomes_an_input_file() -> None:
    """A PDF has a real Responses representation, so it must use it."""
    url = "data:application/pdf;base64,aGVsbG8="

    assert _sole_user_content_part(url) == {
        "type": "input_file",
        "filename": "attachment.pdf",
        "file_data": url,
    }


@pytest.mark.parametrize(
    "mime_type",
    [
        pytest.param("audio/ogg", id="telegram-voice-note"),
        pytest.param("video/mp4", id="video"),
    ],
)
def test_unreadable_media_becomes_a_text_note_naming_the_type(mime_type: str) -> None:
    """Audio and video have no Responses representation.

    Forcing them into `input_image` -- which is what happens if the MIME type is
    not inspected -- sends the API a malformed image. The turn has to stay
    intelligible instead, so the model can ask or delegate.
    """
    part = _sole_user_content_part(f"data:{mime_type};base64,aGVsbG8=")

    assert part["type"] == "input_text"
    text = part["text"]
    assert isinstance(text, str)
    assert mime_type in text
    assert "base64" not in text


def test_plain_url_without_a_data_uri_is_still_an_image() -> None:
    """Only images are fetchable by URL, so an untyped URL means an image."""
    url = "https://example.com/photo.png"

    assert _sole_user_content_part(url) == {"type": "input_image", "image_url": url}
