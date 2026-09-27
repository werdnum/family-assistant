"""Conversation-list search over message content (``get_conversation_summaries``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text
from sqlalchemy.sql import func as sql_func

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import (
    MESSAGE_CONTENT_SEARCH_CONFIG,
    MESSAGE_CONTENT_TSVECTOR,
    message_history_table,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

OWNER = "search_owner"
BASE_TIME = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)


async def _add_exchange(
    db: Database,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    *,
    minutes: int,
    owner: str = OWNER,
) -> None:
    timestamp = BASE_TIME + timedelta(minutes=minutes)
    turn_id = f"{conversation_id}-{minutes}"
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content=user_text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=timestamp,
        turn_id=turn_id,
        processing_profile_id="default",
        user_id=owner,
    )
    await db.message_history.add_message(
        AssistantMessage(content=assistant_text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=timestamp,
        turn_id=turn_id,
        processing_profile_id="default",
        user_id=None,
    )


async def _seed(db: Database) -> None:
    await _add_exchange(
        db,
        "conv_passport",
        "I need to renew the passport before our trip to Japan",
        "The renewal form is online; allow six weeks.",
        minutes=0,
    )
    await _add_exchange(
        db, "conv_passport", "Thanks, that's all", "You're welcome!", minutes=5
    )
    await _add_exchange(
        db,
        "conv_groceries",
        "Add milk and eggs to the shopping list",
        "Added milk and eggs.",
        minutes=10,
    )
    await _add_exchange(
        db,
        "conv_foreign",
        "My passport expired too",
        "Let's renew it.",
        minutes=15,
        owner="somebody_else",
    )


async def _search(db: Database, query: str) -> tuple[dict[str, dict], int]:
    summaries, total = await db.message_history.get_conversation_summaries(
        limit=100,
        include_subconversations=False,
        owner_user_ids={OWNER},
        search_query=query,
    )
    return {s["conversation_id"]: dict(s) for s in summaries}, total


@pytest.mark.asyncio
async def test_search_matches_earlier_messages_not_just_the_preview(
    db_engine: AsyncEngine,
) -> None:
    """The subject lives in the first exchange; the preview is the last reply."""
    db = Database(engine=db_engine)
    await _seed(db)

    results, total = await _search(db, "passport")

    assert list(results) == ["conv_passport"]
    assert total == 1
    summary = results["conv_passport"]
    assert summary["last_message"] == "You're welcome!"
    assert summary["message_count"] == 4
    assert summary["match_excerpt"] is not None
    assert "passport" in summary["match_excerpt"]


@pytest.mark.asyncio
async def test_search_requires_every_word_across_the_conversation(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _seed(db)

    # "Japan" is in the user's message and "weeks" in the reply.
    both, _ = await _search(db, "Japan weeks")
    assert list(both) == ["conv_passport"]

    missing_word, total = await _search(db, "passport milk")
    assert missing_word == {}
    assert total == 0


@pytest.mark.asyncio
async def test_search_is_case_insensitive_and_matches_word_prefixes(
    db_engine: AsyncEngine,
) -> None:
    """A partly typed word finds the conversation while the user is still typing."""
    db = Database(engine=db_engine)
    await _seed(db)

    results, _ = await _search(db, "SHOPP")

    assert list(results) == ["conv_groceries"]


@pytest.mark.asyncio
async def test_search_keeps_other_owners_conversations_out(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _seed(db)

    results, _ = await _search(db, "expired")

    assert results == {}


@pytest.mark.asyncio
async def test_search_without_words_lists_everything(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await _seed(db)

    results, total = await _search(db, "  ?! ")

    assert set(results) == {"conv_passport", "conv_groceries"}
    assert total == 2
    assert all(summary["match_excerpt"] is None for summary in results.values())


@pytest.mark.asyncio
async def test_search_treats_punctuation_as_word_separators(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _seed(db)

    results, _ = await _search(db, "renew, the passport!")

    assert list(results) == ["conv_passport"]


@pytest.mark.asyncio
async def test_content_search_can_use_the_gin_index_on_postgres(
    db_engine: AsyncEngine,
) -> None:
    """The index exists and its expression matches the one the search uses.

    An index whose expression differs in any way (a bound config parameter, a
    ``coalesce``) is silently ignored by the planner, so check the plan.
    """
    if db_engine.dialect.name != "postgresql":
        pytest.skip("The full-text index is PostgreSQL-only")
    statement = select(message_history_table.c.conversation_id).where(
        MESSAGE_CONTENT_TSVECTOR.bool_op("@@")(
            sql_func.to_tsquery(MESSAGE_CONTENT_SEARCH_CONFIG, "passport:*")
        )
    )
    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        compiled = statement.compile(
            dialect=conn.dialect, compile_kwargs={"literal_binds": True}
        )
        plan = (await conn.execute(text(f"EXPLAIN {compiled}"))).scalars().all()

    assert any("ix_message_history_content_fts_gin" in line for line in plan), plan


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["alice@example.com", "alice", "3.25", "(3.25)"],
)
async def test_search_finds_addresses_and_numbers_as_typed(
    db_engine: AsyncEngine, query: str
) -> None:
    """Text PostgreSQL indexes as one token (an address, a decimal) is found whole."""
    db = Database(engine=db_engine)
    await _seed(db)
    await _add_exchange(
        db,
        "conv_invoice",
        "Forward the invoice to alice@example.com please",
        "Sent. The total was 3.25 after the discount.",
        minutes=20,
    )

    results, _ = await _search(db, query)

    assert list(results) == ["conv_invoice"]


@pytest.mark.asyncio
async def test_search_matches_whole_words_by_prefix_only_on_postgres(
    db_engine: AsyncEngine,
) -> None:
    """Full-text search matches the start of a word, not text inside one."""
    if db_engine.dialect.name != "postgresql":
        pytest.skip("SQLite falls back to substring matching")
    db = Database(engine=db_engine)
    await _seed(db)

    # "newal" is inside "renewal" but starts no word.
    results, _ = await _search(db, "newal")

    assert results == {}
