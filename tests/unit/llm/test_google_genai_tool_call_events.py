"""Test Google GenAI streamed tool call events."""

from collections.abc import AsyncIterator

import pytest
from google.genai import types

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


@pytest.mark.asyncio
async def test_tool_call_events_are_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A streamed SDK function call retains its name, arguments, and signature."""
    client = GoogleGenAIClient(
        api_key="test_key_for_unit_tests",
        model="gemini-3.8-flash",
        use_interactions_api=False,
    )
    signature = b"signature_for_execute_script"
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name="execute_script",
                                args={"script": "print(1 + 1)"},
                            ),
                            thought_signature=signature,
                        )
                    ],
                )
            )
        ]
    )

    async def stream() -> AsyncIterator[types.GenerateContentResponse]:
        yield response

    async def generate_content_stream(
        *,
        model: str,
        contents: object,
        config: object = None,
    ) -> AsyncIterator[types.GenerateContentResponse]:
        return stream()

    monkeypatch.setattr(
        client.client.aio.models, "generate_content_stream", generate_content_stream
    )

    events = [
        event
        async for event in client.generate_response_stream(
            messages=[UserMessage(content="Calculate 1+1")]
        )
    ]

    tool_events = [event for event in events if event.type == "tool_call"]
    assert len(tool_events) == 1
    tool_call = tool_events[0].tool_call
    assert tool_call is not None
    assert tool_call.function.name == "execute_script"
    assert tool_call.function.arguments == {"script": "print(1 + 1)"}
    assert tool_call.provider_metadata is not None
    assert tool_call.provider_metadata.thought_signature is not None
    assert tool_call.provider_metadata.thought_signature.to_google_format() == signature
