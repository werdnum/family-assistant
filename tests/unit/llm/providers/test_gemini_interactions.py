"""Unit tests for Gemini chat on the Interactions API.

The stream tests replay the event sequence the live API sent for a turn that
searched for and called a tool (gemini-3.8-flash, recorded 2026-11-19).
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, cast

import pytest
from google.genai import types

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.google_types import (
    GeminiProviderMetadata,
    GeminiThoughtSignature,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.gemini_interactions import (
    SKIP_SIGNATURE,
    InteractionStepAccumulator,
    UnrepresentableInteractionInputError,
    contents_to_steps,
    function_tools,
    interaction_generation_settings,
    interaction_steps_metadata,
    read_steps,
    tool_choice_setting,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient

if TYPE_CHECKING:
    from family_assistant.json_schema import JsonSchemaObject
    from family_assistant.llm import JsonObject
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition

MODEL = "gemini-3.8-flash"


def _client(
    *,
    enable_google_search: bool = False,
    enable_url_context: bool = False,
    enable_computer_use: bool = False,
) -> GoogleGenAIClient:
    return GoogleGenAIClient(
        api_key="test-key",
        model=MODEL,
        enable_google_search=enable_google_search,
        enable_url_context=enable_url_context,
        enable_computer_use=enable_computer_use,
    )


def _input(client: GoogleGenAIClient, messages: list[LLMMessage]) -> list[JsonObject]:
    settings = client._interaction_settings
    assert settings is not None
    return client._interaction_chat_request(messages, None, None, settings)["input"]


def _at(value: object, *path: str | int) -> object:
    """The value at *path* in nested JSON, failing the test if it is absent."""
    for key in path:
        if isinstance(key, int):
            assert isinstance(value, list)
            value = cast("list[object]", value)[key]
        else:
            assert isinstance(value, dict)
            value = cast("dict[str, object]", value)[key]
    return value


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments=arguments),
    )


class TestRenderingHistory:
    def test_text_from_the_user_is_a_user_input_step(self) -> None:
        steps = _input(_client(), [UserMessage(content="Hello")])

        assert steps == [
            {"type": "user_input", "content": [{"type": "text", "text": "Hello"}]}
        ]

    def test_recorded_steps_are_replayed_verbatim(self) -> None:
        recorded: list[JsonObject] = [
            {"type": "thought", "signature": "c2ln"},
            {
                "type": "function_call",
                "id": "call_1",
                "name": "get_weather",
                "arguments": {"city": "Sydney"},
            },
        ]
        messages: list[LLMMessage] = [
            UserMessage(content="Weather?"),
            AssistantMessage(
                tool_calls=[_call("call_1", "get_weather", {"city": "Sydney"})],
                provider_metadata=interaction_steps_metadata(recorded),
            ),
            ToolMessage(tool_call_id="call_1", name="get_weather", content="22C"),
        ]

        steps = _input(_client(), messages)

        assert steps[1:3] == recorded
        assert steps[3] == {
            "type": "function_result",
            "call_id": "call_1",
            "name": "get_weather",
            "result": [{"type": "text", "text": "22C"}],
        }

    def test_a_tool_result_takes_the_name_of_the_call_it_answers(self) -> None:
        messages: list[LLMMessage] = [
            UserMessage(content="Weather?"),
            AssistantMessage(tool_calls=[_call("call_1", "get_weather", {})]),
            ToolMessage(tool_call_id="call_1", name="mislabelled", content="22C"),
        ]

        steps = _input(_client(), messages)

        assert steps[2]["name"] == "get_weather"

    def test_a_generate_content_signature_signs_the_replayed_call(self) -> None:
        signature = b"gc-signature"
        call = _call("call_1", "get_weather", {"city": "Sydney"})
        call = ToolCallItem(
            id=call.id,
            type="function",
            function=call.function,
            provider_metadata=GeminiProviderMetadata(
                thought_signature=GeminiThoughtSignature(signature)
            ).to_dict(),
        )

        steps = _input(
            _client(),
            [UserMessage(content="Weather?"), AssistantMessage(tool_calls=[call])],
        )

        assert steps[1]["signature"] == base64.b64encode(signature).decode()

    def test_an_unsigned_call_gets_the_skip_signature(self) -> None:
        steps = _input(
            _client(),
            [
                UserMessage(content="Weather?"),
                AssistantMessage(tool_calls=[_call("call_1", "get_weather", {})]),
            ],
        )

        assert steps[1]["signature"] == SKIP_SIGNATURE

    def test_assistant_text_from_another_provider_is_model_output(self) -> None:
        steps = _input(
            _client(),
            [UserMessage(content="Hi"), AssistantMessage(content="Hello there")],
        )

        assert steps[1] == {
            "type": "model_output",
            "content": [{"type": "text", "text": "Hello there"}],
        }

    def test_a_system_message_is_rendered_as_a_user_input(self) -> None:
        steps = _input(_client(), [SystemMessage(content="Be brief.")])

        assert steps[0]["type"] == "user_input"
        assert "Be brief." in json.dumps(steps[0])


class TestContentTranslation:
    def test_an_image_carries_the_configured_resolution(self) -> None:
        content = types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=b"png", mime_type="image/png")],
        )

        steps = contents_to_steps([content], media_resolution="high")

        assert steps[0]["content"] == [
            {
                "type": "image",
                "mime_type": "image/png",
                "data": base64.b64encode(b"png").decode(),
                "resolution": "high",
            }
        ]

    def test_inline_text_becomes_a_text_block(self) -> None:
        content = types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=b"line one", mime_type="text/plain")],
        )

        steps = contents_to_steps([content], media_resolution="high")

        assert steps[0]["content"] == [{"type": "text", "text": "line one"}]

    def test_a_pdf_is_a_document(self) -> None:
        content = types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=b"%PDF", mime_type="application/pdf")],
        )

        steps = contents_to_steps([content], media_resolution=None)

        assert _at(steps, 0, "content", 0, "type") == "document"

    def test_a_multimodal_tool_result_keeps_its_media(self) -> None:
        part = types.Part(
            function_response=types.FunctionResponse(
                id="call_1",
                name="get_photo",
                response={"result": "a photo"},
                parts=[
                    types.FunctionResponsePart(
                        inline_data=types.FunctionResponseBlob(
                            mime_type="image/jpeg", data=b"jpg"
                        )
                    )
                ],
            )
        )

        steps = contents_to_steps(
            [types.Content(role="user", parts=[part])], media_resolution=None
        )

        assert steps[0]["result"] == [
            {"type": "text", "text": "a photo"},
            {
                "type": "image",
                "mime_type": "image/jpeg",
                "data": base64.b64encode(b"jpg").decode(),
            },
        ]

    def test_a_part_with_no_interactions_form_is_refused(self) -> None:
        part = types.Part(
            executable_code=types.ExecutableCode(
                code="print(1)", language=types.Language.PYTHON
            )
        )

        with pytest.raises(UnrepresentableInteractionInputError):
            contents_to_steps(
                [types.Content(role="user", parts=[part])], media_resolution=None
            )


class TestToolSchemas:
    def test_a_nested_schema_goes_through_unflattened(self) -> None:
        parameters: JsonSchemaObject = {
            "type": "object",
            "properties": {
                "event": {
                    "type": "object",
                    "properties": {"kind": {"type": "string", "enum": ["a", "b"]}},
                }
            },
        }

        tools = function_tools([
            {
                "type": "function",
                "function": {"name": "f", "description": "d", "parameters": parameters},
            }
        ])

        assert tools[0]["parameters"] == parameters

    def test_a_type_that_is_not_json_schema_is_dropped(self) -> None:
        tools = function_tools([
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "uuid", "description": "An id"}},
                    },
                },
            }
        ])

        assert tools[0]["parameters"] == {
            "type": "object",
            "properties": {"id": {"description": "An id"}},
        }

    def test_tuple_form_items_become_prefix_items(self) -> None:
        tools = function_tools([
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pair": {
                                "type": "array",
                                "items": [{"type": "string"}, {"type": "integer"}],
                            }
                        },
                    },
                },
            }
        ])

        assert _at(tools, 0, "parameters", "properties", "pair") == {
            "type": "array",
            "prefixItems": [{"type": "string"}, {"type": "integer"}],
        }

    def test_enum_values_are_data_and_left_alone(self) -> None:
        tools = function_tools([
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "d",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["type", "items"]}
                        },
                        "default": {"type": "not-a-type"},
                    },
                },
            }
        ])

        assert _at(tools, 0, "parameters", "default") == {"type": "not-a-type"}


@pytest.mark.parametrize(
    ("tool_choice", "expected"),
    [
        ("auto", None),
        (None, None),
        ("none", "none"),
        ("required", "any"),
        ("get_weather", {"allowed_tools": {"mode": "any", "tools": ["get_weather"]}}),
    ],
)
def test_tool_choice_maps_to_interactions(
    tool_choice: str | None, expected: object
) -> None:
    assert tool_choice_setting(tool_choice) == expected


def test_tool_choice_reaches_the_request_generation_config() -> None:
    client = _client()
    settings = client._interaction_settings
    assert settings is not None
    tool: ToolDefinition = {
        "type": "function",
        "function": {"name": "get_weather", "description": "d", "parameters": {}},
    }

    request = client._interaction_chat_request(
        [UserMessage(content="hi")], [tool], "required", settings
    )

    assert request["generation_config"]["tool_choice"] == "any"
    assert [t["name"] for t in request["tools"]] == ["get_weather"]


def test_grounding_tools_are_declared_as_interactions_tools() -> None:
    client = _client(enable_google_search=True, enable_url_context=True)
    settings = client._interaction_settings
    assert settings is not None

    request = client._interaction_chat_request(
        [UserMessage(content="hi")], None, None, settings
    )

    assert request["tools"] == [{"type": "url_context"}, {"type": "google_search"}]


class TestGenerationSettings:
    def test_thinking_and_sampling_parameters_map_across(self) -> None:
        settings = interaction_generation_settings(
            {
                "temperature": 0.2,
                "max_output_tokens": 512,
                "thinking_config": {"thinking_level": "LOW", "include_thoughts": True},
                "media_resolution": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            },
            model=MODEL,
        )

        assert settings.generation_config == {
            "temperature": 0.2,
            "max_output_tokens": 512,
            "thinking_level": "low",
            "thinking_summaries": "auto",
        }
        assert settings.media_resolution == "high"

    def test_a_parameter_without_an_equivalent_fails_naming_the_model(self) -> None:
        with pytest.raises(ValueError, match=f"'{MODEL}'.*thinking_budget"):
            interaction_generation_settings(
                {"thinking_config": {"thinking_budget": 1024}}, model=MODEL
            )

    def test_the_client_validates_its_parameters_at_construction(self) -> None:
        safety = {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}
        with pytest.raises(ValueError, match="safety_settings"):
            GoogleGenAIClient(
                api_key="test-key",
                model=MODEL,
                model_parameters={MODEL: {"safety_settings": [safety]}},
            )

    def test_use_interactions_api_false_keeps_generate_content(self) -> None:
        client = GoogleGenAIClient(
            api_key="test-key",
            model=MODEL,
            model_parameters={MODEL: {"use_interactions_api": False}},
        )

        assert client._interaction_settings is None

    def test_use_interactions_api_must_be_a_boolean(self) -> None:
        with pytest.raises(ValueError, match="use_interactions_api"):
            GoogleGenAIClient(
                api_key="test-key",
                model=MODEL,
                model_parameters={MODEL: {"use_interactions_api": "no"}},
            )

    def test_computer_use_stays_on_generate_content(self) -> None:
        assert _client(enable_computer_use=True)._interaction_settings is None


# A live stream for "Weather in Sydney?", with the long signatures shortened.
RECORDED_STREAM: list[JsonObject] = [
    {"event_type": "interaction.created", "interaction": {"model": MODEL}},
    {"event_type": "interaction.status_update", "status": "in_progress"},
    {
        "event_type": "step.start",
        "index": 0,
        "step": {
            "arguments": {},
            "id": "call_a",
            "signature": "s0",
            "type": "tool_search_call",
        },
    },
    {
        "event_type": "step.delta",
        "index": 0,
        "delta": {
            "arguments": {"function_names": ["search_weather"]},
            "signature": "s0b",
            "type": "tool_search_call",
        },
    },
    {"event_type": "step.stop", "index": 0},
    {
        "event_type": "step.start",
        "index": 1,
        "step": {"call_id": "call_a", "signature": "s1", "type": "tool_search_result"},
    },
    {
        "event_type": "step.delta",
        "index": 1,
        "delta": {
            "result": [{"name": "default_api:search_weather", "type": "function"}],
            "signature": "s1b",
            "type": "tool_search_result",
        },
    },
    {"event_type": "step.stop", "index": 1},
    {"event_type": "step.start", "index": 2, "step": {"type": "thought"}},
    {
        "event_type": "step.delta",
        "index": 2,
        "delta": {"signature": "s2", "type": "thought_signature"},
    },
    {"event_type": "step.stop", "index": 2},
    {
        "event_type": "step.start",
        "index": 3,
        "step": {
            "arguments": {},
            "id": "call_b",
            "name": "search_weather",
            "type": "function_call",
        },
    },
    {
        "event_type": "step.delta",
        "index": 3,
        "delta": {"arguments": '{"city":', "type": "arguments_delta"},
    },
    {
        "event_type": "step.delta",
        "index": 3,
        "delta": {"arguments": '"Sydney"}', "type": "arguments_delta"},
    },
    {"event_type": "step.stop", "index": 3},
    {"event_type": "step.start", "index": 4, "step": {"type": "model_output"}},
    {
        "event_type": "step.delta",
        "index": 4,
        "delta": {"text": "Checking", "type": "text"},
    },
    {
        "event_type": "step.delta",
        "index": 4,
        "delta": {"text": " now.", "type": "text"},
    },
    {"event_type": "step.stop", "index": 4},
]


def _fold(events: list[JsonObject]) -> tuple[InteractionStepAccumulator, list[str]]:
    accumulator = InteractionStepAccumulator()
    texts: list[str] = []
    for event in events:
        index = event.get("index")
        if not isinstance(index, int):
            continue
        if event["event_type"] == "step.start":
            step = event["step"]
            assert isinstance(step, dict)
            accumulator.start(index, step)
        elif event["event_type"] == "step.delta":
            delta = event["delta"]
            assert isinstance(delta, dict)
            if text := accumulator.delta(index, delta):
                texts.append(text)
        elif event["event_type"] == "step.stop":
            accumulator.stop(index)
    return accumulator, texts


class TestStreamFolding:
    def test_steps_are_rebuilt_in_their_non_streamed_shape(self) -> None:
        accumulator, _ = _fold(RECORDED_STREAM)

        assert accumulator.steps == [
            {
                "arguments": {"function_names": ["search_weather"]},
                "id": "call_a",
                "signature": "s0b",
                "type": "tool_search_call",
            },
            {
                "call_id": "call_a",
                "result": [{"name": "default_api:search_weather", "type": "function"}],
                "signature": "s1b",
                "type": "tool_search_result",
            },
            {"type": "thought", "signature": "s2"},
            {
                "arguments": {"city": "Sydney"},
                "id": "call_b",
                "name": "search_weather",
                "type": "function_call",
            },
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "Checking now."}],
            },
        ]

    def test_text_deltas_are_returned_to_show_as_they_arrive(self) -> None:
        _, texts = _fold(RECORDED_STREAM)

        assert texts == ["Checking", " now."]

    def test_the_rebuilt_steps_read_back_as_text_and_tool_calls(self) -> None:
        accumulator, _ = _fold(RECORDED_STREAM)

        output = read_steps(accumulator.steps)

        assert output.content == "Checking now."
        assert output.tool_calls == [("call_b", "search_weather", {"city": "Sydney"})]

    def test_thought_summaries_are_collected(self) -> None:
        accumulator = InteractionStepAccumulator()
        accumulator.start(0, {"type": "thought"})
        accumulator.delta(
            0, {"type": "thought_summary", "content": {"type": "text", "text": "Plan"}}
        )
        accumulator.delta(
            0, {"type": "thought_summary", "content": {"type": "text", "text": " more"}}
        )
        accumulator.stop(0)

        assert read_steps(accumulator.steps).thought_summaries == ["Plan more"]
