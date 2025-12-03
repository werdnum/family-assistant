"""Test that Google GenAI client correctly emits tool_call events."""

import os

import pytest

from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


@pytest.mark.asyncio
@pytest.mark.llm_integration
async def test_tool_call_events_are_emitted() -> None:
    """Test that tool_call events are emitted when LLM returns function calls."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        pytest.skip("GEMINI_API_KEY not set")

    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.5-flash",
    )

    messages = [
        SystemMessage(content="You are a helpful assistant."),
        UserMessage(content="use Python to calculate 1+1"),
    ]

    tools = [
        {
            "type": "function",
            "function": {
                "name": "execute_script",
                "description": "Execute Python code",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "string",
                            "description": "Python code to execute",
                        }
                    },
                    "required": ["script"],
                },
            },
        }
    ]

    # Collect events
    events = []
    async for event in client.generate_response_stream(messages=messages, tools=tools):
        events.append(event)
        print(f"Event: {event.type}")
        if event.type == "tool_call" and event.tool_call:
            print(f"  Tool call: {event.tool_call.function.name}")

    # Check that we got at least one tool_call event
    tool_call_events = [e for e in events if e.type == "tool_call"]
    assert len(tool_call_events) > 0, (
        f"Expected tool_call events, got: {[e.type for e in events]}"
    )

    # Check that the tool call has provider_metadata with thought_signature
    tool_call_item = tool_call_events[0].tool_call
    assert tool_call_item is not None, "tool_call should not be None"
    assert tool_call_item.provider_metadata is not None
    assert tool_call_item.provider_metadata.thought_signature is not None
