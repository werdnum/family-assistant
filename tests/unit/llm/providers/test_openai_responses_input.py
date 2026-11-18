"""Regression tests for OpenAI Responses API input conversion."""

import json
from collections.abc import AsyncIterator

import pytest
from openai.types.responses import ResponseStreamEvent
from pydantic import TypeAdapter

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    AssistantMessage,
    AttachmentContentPart,
    ImageUrlContentPart,
    LLMMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.tools.types import ToolAttachment


@pytest.fixture(autouse=True)
def _clear_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


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

    Without this metadata `stream_error_to_exception` can only raise a bare
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


def _sole_user_content_part(
    url: str, attachment_id: str | None = None
) -> dict[str, object]:
    """Convert a single media part through a direct-OpenAI client."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-terra")
    items = client._messages_to_responses_input([
        UserMessage(
            content=[
                ImageUrlContentPart(
                    type="image_url",
                    image_url={"url": url},
                    attachment_id=attachment_id,
                )
            ]
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


def test_unreadable_media_note_hands_off_the_attachment_by_id() -> None:
    """The note is only actionable if it names the attachment to delegate.

    There is no tool that lists a conversation's attachments, so an id-less note
    leaves the model unable to refer to the file at all.
    """
    part = _sole_user_content_part(
        "data:audio/ogg;base64,aGVsbG8=", attachment_id="att-1234"
    )

    text = part["text"]
    assert isinstance(text, str)
    assert "att-1234" in text
    assert "delegate_to_service" in text
    assert "media_analyst" in text


def test_unreadable_media_without_an_id_asks_the_user_instead() -> None:
    """With no id there is nothing to delegate, so it must not invent a handoff."""
    part = _sole_user_content_part("data:audio/ogg;base64,aGVsbG8=")

    text = part["text"]
    assert isinstance(text, str)
    assert "delegate_to_service" not in text
    assert "None" not in text


def _injected_responses_parts(mime_type: str) -> list[dict[str, object]]:
    """Run an attachment through injection and the Responses conversion."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-terra")
    attachment = ToolAttachment(
        content=b"hello",
        mime_type=mime_type,
        attachment_id="att-9",
        description="a file",
    )
    message = client.create_attachment_injection(attachment)
    items = client._messages_to_responses_input([message])
    content = items[0]["content"]
    assert isinstance(content, list)
    # The first part is the "[System: File from previous tool response]" preamble.
    return content[1:]


def test_injected_pdf_reaches_the_responses_api_as_a_file() -> None:
    """Web chat sends PDFs down the injection path, not as image_url parts.

    `chat_api.py` routes every non-image upload through `attachment_content`, so
    this path — not the chat content-part path — is what a web user's PDF takes.
    It used to be replaced with `[PDF Document: ...]` placeholder text on a model
    that reads PDFs natively, losing the document silently.
    """
    parts = _injected_responses_parts("application/pdf")

    assert [part["type"] for part in parts] == ["input_file"]


def test_injected_audio_carries_the_handoff_id() -> None:
    """Audio has no Responses representation, so it must name itself instead.

    Without the attachment id reaching this path the note would tell the model
    to delegate a file it cannot identify.
    """
    parts = _injected_responses_parts("audio/ogg")

    assert [part["type"] for part in parts] == ["input_text"]
    text = parts[0]["text"]
    assert isinstance(text, str)
    assert "att-9" in text
    assert "media_analyst" in text


def test_injected_pdf_on_a_compatible_endpoint_stays_text() -> None:
    """Chat Completions has no `input_file`, so the text degradation stands there.

    A `base_url` backend implements Chat Completions only. Sending it a PDF data
    URI as an image part would be a malformed request rather than a graceful one.
    """
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-terra",
        base_url="https://openrouter.ai/api/v1",
    )
    attachment = ToolAttachment(
        content=b"hello",
        mime_type="application/pdf",
        attachment_id="att-9",
        description="a file",
    )
    message = client.create_attachment_injection(attachment)
    serialized = client._to_chat_completions_message(message)

    assert client._uses_responses_api() is False
    parts = serialized["content"]
    assert isinstance(parts, list)
    assert [part["type"] for part in parts] == ["text", "text"]
    assert "PDF Document" in parts[1]["text"]


_PARALLEL_SEARCH_CALLS = [
    ToolCallItem(
        id=f"call_search_{index}",
        type="function",
        function=ToolCallFunction(
            name="find_tasks", arguments=json.dumps({"query": query})
        ),
    )
    for index, query in enumerate(["invoice", "school", "dentist"])
]


def _search_turn(
    response_output: list[dict[str, object]],
    *,
    content: str = "",
) -> list[LLMMessage]:
    """An assistant turn of three parallel calls, its outputs, and a follow-up."""
    return [
        UserMessage(content="Review the inbox"),
        AssistantMessage(
            content=content,
            tool_calls=list(_PARALLEL_SEARCH_CALLS),
            provider_metadata={
                "openai_response_output": response_output,
                "openai_response_stored": False,
            },
        ),
        *[
            ToolMessage(tool_call_id=call.id, name="find_tasks", content="[]")
            for call in _PARALLEL_SEARCH_CALLS
        ],
        UserMessage(content="Carry on"),
    ]


def _assert_every_output_has_its_call(input_items: list[dict[str, object]]) -> None:
    """The invariant the Responses API enforces with an HTTP 400."""
    seen_calls: list[object] = []
    for item in input_items:
        if item.get("type") == "function_call":
            seen_calls.append(item["call_id"])
        elif item.get("type") == "function_call_output":
            assert item["call_id"] in seen_calls, item
    assert len(seen_calls) == len(set(seen_calls)), seen_calls


def _function_call_item(call: ToolCallItem) -> dict[str, object]:
    return {
        "type": "function_call",
        "id": f"fc_{call.id}",
        "status": "completed",
        "call_id": call.id,
        "name": call.function.name,
        "arguments": call.function.arguments,
    }


def test_empty_stored_output_still_replays_the_tool_calls() -> None:
    """A stream that completed with ``output: []`` must not orphan its outputs.

    The shape of the history that wedged a thread: three streamed tool calls
    recorded on the message, an empty final output in the provider metadata,
    and every later request rejected with "No tool call found for function
    call output".
    """
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input(_search_turn([]))

    _assert_every_output_has_its_call(input_items)
    assert [item["call_id"] for item in input_items[1:4]] == [
        call.id for call in _PARALLEL_SEARCH_CALLS
    ]


def test_partially_stored_output_adds_only_the_missing_calls() -> None:
    """Stored items are replayed verbatim; only what they lack is rebuilt."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    reasoning: dict[str, object] = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque",
    }
    stored = [reasoning, _function_call_item(_PARALLEL_SEARCH_CALLS[0])]

    input_items = client._messages_to_responses_input(_search_turn(stored))

    _assert_every_output_has_its_call(input_items)
    assert input_items[1] == reasoning
    assert input_items[2]["id"] == "fc_call_search_0"
    assert [item.get("call_id") for item in input_items[3:5]] == [
        "call_search_1",
        "call_search_2",
    ]
    assert "id" not in input_items[3]


def test_complete_stored_output_is_replayed_unchanged() -> None:
    """Nothing is added when the stored output already covers the turn."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    message_item: dict[str, object] = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Searching", "annotations": []}],
    }
    stored = [message_item, *map(_function_call_item, _PARALLEL_SEARCH_CALLS)]

    input_items = client._messages_to_responses_input(
        _search_turn(stored, content="Searching")
    )

    assert input_items[1:5] == [
        {key: value for key, value in item.items() if key != "status"}
        for item in stored
    ]
    assert input_items[5]["type"] == "function_call_output"


def test_empty_stored_output_still_replays_the_text() -> None:
    """The assistant's words survive an empty stored output too."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    input_items = client._messages_to_responses_input(
        _search_turn([], content="Let me look")
    )

    assert input_items[1]["type"] == "message"
    assert input_items[1]["content"] == [
        {"type": "output_text", "text": "Let me look", "annotations": []}
    ]
    _assert_every_output_has_its_call(input_items)


def _streamed_call_frames(
    final_output: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Responses stream frames: three finished calls, then a terminal response."""
    frames: list[dict[str, object]] = [
        {
            "type": "response.output_item.done",
            "output_index": index,
            "sequence_number": index,
            "item": _function_call_item(call),
        }
        for index, call in enumerate(_PARALLEL_SEARCH_CALLS)
    ]
    frames.append({
        "type": "response.completed",
        "sequence_number": len(frames),
        "response": {
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "model": "gpt-5.6-sol",
            "status": "completed",
            "output": final_output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    })
    return frames


@pytest.mark.parametrize(
    "final_output",
    [
        pytest.param([], id="empty"),
        pytest.param(
            [_function_call_item(_PARALLEL_SEARCH_CALLS[1])], id="partially-missing"
        ),
        pytest.param(
            [_function_call_item(call) for call in _PARALLEL_SEARCH_CALLS],
            id="complete",
        ),
    ],
)
@pytest.mark.parametrize("live", [True, False], ids=["sdk-stream", "vcr-frames"])
async def test_streamed_calls_survive_an_incomplete_final_output(
    final_output: list[dict[str, object]],
    live: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every finished call reaches the stored output, exactly once."""
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    frames = _streamed_call_frames(final_output)

    if live:
        adapter: TypeAdapter[ResponseStreamEvent] = TypeAdapter(ResponseStreamEvent)
        typed_events = [adapter.validate_python(frame) for frame in frames]

        async def _stream() -> AsyncIterator[ResponseStreamEvent]:
            for event in typed_events:
                yield event

        async def _create(**_params: object) -> AsyncIterator[ResponseStreamEvent]:
            return _stream()

        monkeypatch.setattr(client.client.responses, "create", _create)
        events = [
            event
            async for event in client._generate_responses_stream(
                [UserMessage(content="Review the inbox")], None, None
            )
        ]
    else:
        events = [
            event
            async for event in client._emit_events_from_responses_dicts(
                frames, originating_response_stored=False
            )
        ]

    done = events[-1]
    assert done.type == "done"
    assert done.metadata is not None
    provider_metadata = done.metadata.get("provider_metadata")
    assert isinstance(provider_metadata, dict)
    stored = provider_metadata["openai_response_output"]
    assert sorted(item["call_id"] for item in stored) == [
        call.id for call in _PARALLEL_SEARCH_CALLS
    ]


async def test_restored_output_keeps_the_streamed_order() -> None:
    """A reasoning item stays ahead of the calls it led to.

    Replayed history has to keep output items in the order the model produced
    them; appending what the terminal output omitted would put the reasoning
    after a call that followed it.
    """
    client = OpenAIClient(api_key="test-key", model="gpt-5.6-sol")
    reasoning: dict[str, object] = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque",
    }
    frames = _streamed_call_frames([_function_call_item(_PARALLEL_SEARCH_CALLS[1])])
    frames.insert(
        0,
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "sequence_number": 0,
            "item": reasoning,
        },
    )

    events = [
        event
        async for event in client._emit_events_from_responses_dicts(
            frames, originating_response_stored=False
        )
    ]

    assert events[-1].metadata is not None
    provider_metadata = events[-1].metadata.get("provider_metadata")
    assert isinstance(provider_metadata, dict)
    assert [item["id"] for item in provider_metadata["openai_response_output"]] == [
        "rs_1",
        *(f"fc_{call.id}" for call in _PARALLEL_SEARCH_CALLS),
    ]
