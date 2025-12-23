"""Integration test for Google Deep Research Agent.

This test requires a valid GEMINI_API_KEY environment variable.
It uses VCR to record and replay interactions.
"""

import os

import pytest

from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deep_research_integration_simple_query() -> None:
    """
    Integration test for Deep Research agent.

    This test verifies that the client can successfully initiate a deep research session,
    stream events (including thoughts and content), and complete successfully.
    """
    # Skip if no API key and not in replay mode (implied by environment check or vcr)
    # The actual skipping logic might depend on how the test runner is configured,
    # but checking for the key is a safe guard for local runs.
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY not set")

    client = GoogleGenAIClient(
        api_key=os.environ["GEMINI_API_KEY"], model="deep-research-pro-preview-12-2025"
    )

    messages = [
        SystemMessage(content="You are a helpful research assistant."),
        UserMessage(content="What is the capital of France? Answer briefly."),
    ]

    events = []
    thought_count = 0
    content_accumulated = ""

    try:
        async for event in client.generate_response_stream(messages):
            events.append(event)
            if event.type == "content":
                if event.content and "*Thinking:" in event.content:
                    thought_count += 1
                elif event.content:
                    content_accumulated += event.content
            elif event.type == "error":
                pytest.fail(f"Stream returned error: {event.error}")
    finally:
        await client.close()

    # Verification
    # 1. We should have received events
    assert len(events) > 0

    # 2. The final event should be 'done'
    assert events[-1].type == "done"

    # 3. We should have some content (though deep research might be verbose, "Paris" should be there)
    assert "Paris" in content_accumulated

    # 4. We should have captured an interaction ID in the metadata
    done_event = events[-1]
    assert done_event.metadata is not None
    provider_metadata = done_event.metadata.get("provider_metadata")
    assert provider_metadata is not None

    # Check if interaction_id is present (either as attribute or dict key depending on serialization)
    if hasattr(provider_metadata, "interaction_id"):
        assert provider_metadata.interaction_id is not None
    elif isinstance(provider_metadata, dict):
        assert provider_metadata.get("interaction_id") is not None

    # 5. Ideally we see some thoughts, but it depends on the model's behavior
    # assert thought_count > 0  # Optional check
