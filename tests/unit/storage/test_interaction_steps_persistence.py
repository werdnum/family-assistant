"""Gemini Interactions replay state must survive the database round trip.

The Interactions API requires a response's steps back as it sent them,
signatures included, so any mangling between saving an assistant turn and
reading it back for the next request is a 400 on the continuation. These tests
pin the storage layer against that, and feed the round-tripped message back
through the client's rendering to prove the replay reproduces the steps.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import JsonObject, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage
from family_assistant.llm.providers.gemini_interactions import (
    interaction_replay_metadata,
    stored_replay_steps,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.storage.database import Database

SIGNATURE = "EqMICqAIAWkUfRNx1SZmIiUvOzP7c2b2J755aSgkRoTRtGqP5QItCrlHMJk+/=" * 4
STEPS: list[JsonObject] = [
    {
        "type": "tool_search_call",
        "id": "call_a",
        "arguments": {"function_names": ["get_weather"]},
        "signature": SIGNATURE,
    },
    {
        "type": "thought",
        "signature": SIGNATURE,
        "summary": [{"type": "text", "text": "Plan"}],
    },
    {
        "type": "function_call",
        "id": "call_b",
        "name": "get_weather",
        "arguments": {"city": "Sydney", "days": 2, "units": None},
    },
]


@pytest.fixture
def db_context(db_engine: AsyncEngine) -> Database:
    return Database(engine=db_engine, base_delay=0.01)


async def _round_trip(db_context: Database) -> AssistantMessage:
    turn_id = str(uuid.uuid4())
    await db_context.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="call_b",
                    type="function",
                    function=ToolCallFunction(
                        name="get_weather",
                        arguments={"city": "Sydney", "days": 2, "units": None},
                    ),
                )
            ],
            provider_metadata=interaction_replay_metadata(STEPS),
        ),
        interface_type="web",
        conversation_id=str(uuid.uuid4()),
        timestamp=datetime.now(UTC),
        interface_message_id="msg-interaction",
        turn_id=turn_id,
        thread_root_id=None,
    )
    history = await db_context.message_history.get_by_turn_id(turn_id)
    assistant_messages = [m for m in history if isinstance(m, AssistantMessage)]
    assert len(assistant_messages) == 1
    return assistant_messages[0]


@pytest.mark.asyncio
async def test_interaction_steps_survive_the_database_round_trip(
    db_context: Database,
) -> None:
    restored = await _round_trip(db_context)

    stored = interaction_replay_metadata(STEPS)
    assert stored is not None
    assert stored_replay_steps(restored) == stored["replay_steps"]


@pytest.mark.asyncio
async def test_a_round_tripped_message_replays_its_steps(db_context: Database) -> None:
    restored = await _round_trip(db_context)
    client = GoogleGenAIClient(api_key="test-key", model="gemini-3.8-flash")
    settings = client._interaction_settings
    assert settings is not None

    request = client._interaction_chat_request(
        [restored], None, None, settings, native_retrieval=False
    )

    assert request["input"] == STEPS
