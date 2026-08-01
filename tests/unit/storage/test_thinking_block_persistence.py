"""Anthropic thinking blocks must survive the database round trip byte-exact.

The API verifies the `signature` on a replayed thinking block, so any mangling
between writing an assistant turn and reading it back for the next request turns
into a 400 on the continuation rather than a quiet loss. These tests pin the
storage layer against that, and then feed the round-tripped message back through
the client's conversion to prove the replay path still accepts it.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.storage.context import DatabaseContext, get_db_context

# A signature shaped like the real thing: long, base64-ish, and meaningless if
# a single character shifts.
SIGNATURE = "ErUBCkYIBRgCKkBm2n0pQ7v9XyzAbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/" * 4
THINKING_BLOCK: dict[str, object] = {
    "type": "thinking",
    "thinking": "42 * 17 = 714. I should confirm with the calculate tool.",
    "signature": SIGNATURE,
}
PROVIDER_METADATA: dict[str, object] = {
    "provider": "anthropic",
    "thinking_blocks": [THINKING_BLOCK],
}


@pytest_asyncio.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    context_instance = get_db_context(engine=db_engine, base_delay=0.01)
    async with context_instance as entered_context:
        yield entered_context


async def _round_trip(db_context: DatabaseContext) -> AssistantMessage:
    """Persist an assistant turn with thinking blocks and read it back."""
    turn_id = str(uuid.uuid4())
    await db_context.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="toolu_01abc",
                    type="function",
                    function=ToolCallFunction(
                        name="calculate", arguments='{"expression": "42 * 17"}'
                    ),
                )
            ],
            provider_metadata=PROVIDER_METADATA,
        ),
        interface_type="web",
        conversation_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC),
        interface_message_id="msg-thinking",
        turn_id=turn_id,
        thread_root_id=None,
    )

    history = await db_context.message_history.get_by_turn_id(turn_id)
    assistant_messages = [
        message for message in history if isinstance(message, AssistantMessage)
    ]
    assert len(assistant_messages) == 1
    return assistant_messages[0]


@pytest.mark.asyncio
async def test_thinking_blocks_survive_database_round_trip(
    db_context: DatabaseContext,
) -> None:
    """The stored metadata must come back identical, signature included."""
    restored = await _round_trip(db_context)

    assert restored.provider_metadata == PROVIDER_METADATA
    metadata_dict = restored.provider_metadata
    assert isinstance(metadata_dict, dict)
    assert metadata_dict["thinking_blocks"][0]["signature"] == SIGNATURE


@pytest.mark.asyncio
async def test_round_tripped_message_replays_thinking_first(
    db_context: DatabaseContext,
) -> None:
    """A message read back from storage still converts into a valid turn."""
    restored = await _round_trip(db_context)
    client = AnthropicClient(api_key="test-key", model="claude-sonnet-4-6")

    _system, api_messages = client._convert_messages_to_anthropic_format([restored])

    content = api_messages[0]["content"]
    assert [block["type"] for block in content] == ["thinking", "tool_use"]
    assert content[0] == THINKING_BLOCK
