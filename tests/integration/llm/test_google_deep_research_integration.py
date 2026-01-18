"""Integration test for Google Deep Research Agent.

This test requires a valid GEMINI_API_KEY environment variable.
It makes live API calls to a preview model which may have unstable behavior.
"""

import logging
import os

import pytest

from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deep_research_integration_simple_query() -> None:
    """
    Integration test for Deep Research agent.

    This test verifies that the client can successfully initiate a deep research session,
    stream events (including thoughts and content), and complete successfully.

    NOTE: This test uses a preview model (deep-research-pro-preview-12-2025) which may
    have unstable behavior. If the API returns no content, the test will be skipped
    with a warning rather than failing.
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
    thought_count = 0
    content_accumulated = ""

    try:
        async for event in client.generate_response_stream(messages):
            events.append(event)
            logger.debug(
                f"Received event: type={event.type}, content={event.content[:100] if event.content else None}..."
            )
            if event.type == "content":
                if event.content and "*Thinking:" in event.content:
                    thought_count += 1
                elif event.content:
                    content_accumulated += event.content
            elif event.type == "error":
                pytest.fail(f"Stream returned error: {event.error}")
    finally:
        await client.close()

    # Log summary for debugging
    event_types = [e.type for e in events]
    logger.info(f"Received {len(events)} events: {event_types}")
    logger.info(f"Content accumulated length: {len(content_accumulated)}")
    logger.info(f"Thought count: {thought_count}")

    # Verification
    # 1. We should have received events
    assert len(events) > 0, "No events received from Deep Research API"

    # 2. The final event should be 'done'
    assert events[-1].type == "done", (
        f"Final event type was {events[-1].type}, expected 'done'"
    )

    # 3. Check for content - if none received, skip with warning (API may be unstable)
    if not content_accumulated:
        pytest.skip(
            "Deep Research API returned no content. This may be due to API rate limits, "
            "service issues, or changes in the preview model behavior. "
            f"Events received: {event_types}"
        )

    # 4. We should have some content mentioning Paris
    assert "Paris" in content_accumulated, (
        f"Expected 'Paris' in response but got: {content_accumulated[:500]}..."
    )

    # 5. We should have captured an interaction ID in the metadata
    done_event = events[-1]
    assert done_event.metadata is not None, "Done event missing metadata"
    provider_metadata = done_event.metadata.get("provider_metadata")
    assert provider_metadata is not None, "Missing provider_metadata in done event"

    # Check if interaction_id is present (either as attribute or dict key depending on serialization)
    if hasattr(provider_metadata, "interaction_id"):
        assert provider_metadata.interaction_id is not None
    elif isinstance(provider_metadata, dict):
        assert provider_metadata.get("interaction_id") is not None

    # 6. Ideally we see some thoughts, but it depends on the model's behavior
    # assert thought_count > 0  # Optional check
