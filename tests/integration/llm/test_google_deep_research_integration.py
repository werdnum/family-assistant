"""Live integration test for the Google Deep Research Agent."""

import logging
import os

import pytest

from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.llm_integration
async def test_deep_research_integration_simple_query() -> None:
    """
    Integration test for Deep Research agent.

    This test verifies that the client can successfully initiate a deep research session,
    stream events (including thoughts and content), and complete successfully.

    This test uses a preview model (deep-research-pro-preview-12-2025).
    """
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
    content_accumulated = ""

    try:
        async for event in client.generate_response_stream(messages):
            events.append(event)
            logger.debug(
                f"Received event: type={event.type}, content={event.content[:100] if event.content else None}..."
            )
            if event.type == "content":
                if event.content and "*Thinking:" not in event.content:
                    content_accumulated += event.content
            elif event.type == "error":
                pytest.fail(f"Stream returned error: {event.error}")
    finally:
        await client.close()

    # Log summary for debugging
    event_types = [e.type for e in events]
    logger.info(f"Received {len(events)} events: {event_types}")
    logger.info(f"Content accumulated length: {len(content_accumulated)}")

    # Verification
    # 1. We should have received events
    assert len(events) > 0, "No events received from Deep Research API"

    # 2. The final event should be 'done'
    assert events[-1].type == "done", (
        f"Final event type was {events[-1].type}, expected 'done'"
    )

    # 3. We should have some content mentioning Paris
    assert "Paris" in content_accumulated, (
        f"Expected 'Paris' in response but got: {content_accumulated[:500]}..."
    )

    # 4. We should have captured an interaction ID in the metadata
    done_event = events[-1]
    assert done_event.metadata is not None, "Done event missing metadata"
    provider_metadata = done_event.metadata.get("provider_metadata")
    assert isinstance(provider_metadata, GeminiProviderMetadata)
    assert provider_metadata.interaction_id
