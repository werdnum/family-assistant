"""The database-backed Agent SDK session store."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from claude_agent_sdk.testing import run_session_store_conformance
from sqlalchemy import delete

from family_assistant.processing.claude_agent_sessions import (
    DatabaseSessionStore,
    database_session_store,
)
from family_assistant.storage.claude_agent_sessions import (
    claude_agent_session_entries_table,
    claude_agent_session_turns_table,
)
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.claude_agent_sessions import (
    ClaudeAgentSessionsRepository,
)

if TYPE_CHECKING:
    from claude_agent_sdk import SessionKey, SessionStoreEntry
    from sqlalchemy.ext.asyncio import AsyncEngine

TRANSCRIPT = Path(__file__).parents[2] / (
    "data/claude_agent_sdk/session_store_transcript.jsonl"
)
KEY: SessionKey = {"project_key": "-tmp-claude-agent", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_store_meets_the_sdk_contract(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)

    async def fresh_store() -> DatabaseSessionStore:
        await db.execute(delete(claude_agent_session_entries_table))
        return database_session_store(db)

    await run_session_store_conformance(fresh_store)


@pytest.mark.asyncio
async def test_real_session_transcript_round_trips(db_engine: AsyncEngine) -> None:
    """Entries a real Claude Code session mirrored, appended in two batches."""
    entries = [
        json.loads(line) for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines()
    ]
    store = database_session_store(Database(db_engine))
    half = len(entries) // 2

    await store.append(KEY, entries[:half])
    await store.append(KEY, entries[half:])

    assert await store.load(KEY) == entries


@pytest.mark.asyncio
async def test_awkward_values_round_trip(db_engine: AsyncEngine) -> None:
    """A NUL, which PostgreSQL rejects in JSONB, and a lone surrogate."""
    entry = cast(
        "SessionStoreEntry",
        {
            "type": "user",
            "uuid": "u1",
            "text": "nul\u0000here \ud800 café \U0001f375",
            "numbers": [2**70, 1e308, -0.0, 0.1],
        },
    )
    store = database_session_store(Database(db_engine))

    await store.append(KEY, [entry])

    assert await store.load(KEY) == [entry]


@pytest.mark.asyncio
async def test_a_failed_append_is_remembered(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("database went away")

    monkeypatch.setattr(ClaudeAgentSessionsRepository, "append_entries", fail)
    store = database_session_store(Database(db_engine))

    with pytest.raises(ConnectionError):
        await store.append(KEY, [{"type": "user", "uuid": "u1"}])

    assert store.write_failed


@pytest.mark.asyncio
async def test_idle_sessions_are_deleted_with_their_turns(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    store = database_session_store(db)
    await store.append(KEY, [{"type": "user", "uuid": "u1"}])
    await db.claude_agent_sessions.record_turn(
        session_id=KEY["session_id"],
        turn_id="turn-1",
        interface_type="test",
        conversation_id="conv-1",
        subconversation_id=None,
        processing_profile_id="claude_agent",
        model="claude-opus-5-5",
        effort="high",
        system_prompt_sha256="0" * 64,
        taint_metadata=None,
    )

    await db.claude_agent_sessions.delete_sessions_idle_since(
        datetime.now(UTC) + timedelta(seconds=1)
    )

    assert await store.load(KEY) is None
    assert await db.fetch_all(claude_agent_session_turns_table.select()) == []
