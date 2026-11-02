"""Every write to a synced table moves a commit-ordered cursor.

Clients trust "nothing changed above my cursor" only if no write can skip the
stamp and no cursor can become visible below one a client has already read.
See docs/design/mobile-state-sync.md, "Change tracking".
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage
from family_assistant.storage.conversation_threads import (
    conversation_read_markers_table,
)
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.sync_tracking import (
    sync_tombstones_table,
    sync_wakeup_for,
)


async def _add_message(db: Database, conversation_id: str, text: str = "hi") -> int:
    return await db.message_history.add_message(
        AssistantMessage(content=text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id="alice",
    )


async def _cursor_of(db: Database, internal_id: int) -> int:
    return await db.fetch_value(
        select(message_history_table.c.sync_cursor).where(
            message_history_table.c.internal_id == internal_id
        )
    )


async def test_each_transaction_stamps_a_higher_cursor(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)

    first = await _cursor_of(db, await _add_message(db, "c1"))
    second = await _cursor_of(db, await _add_message(db, "c1"))

    assert 0 < first < second


async def test_rows_written_in_one_transaction_share_its_cursor(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)

    async with db.transaction() as txn:
        first = await txn.message_history.add_message(
            AssistantMessage(content="a"),
            interface_type="web",
            conversation_id="c1",
            timestamp=datetime.now(UTC),
        )
        second = await txn.message_history.add_message(
            AssistantMessage(content="b"),
            interface_type="web",
            conversation_id="c1",
            timestamp=datetime.now(UTC),
        )

    assert await _cursor_of(db, first) == await _cursor_of(db, second)


async def test_a_multi_row_insert_stamps_every_row(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)
    now = datetime.now(UTC)

    await db.execute(
        insert(message_history_table).values([
            {
                "interface_type": "web",
                "conversation_id": "c1",
                "timestamp": now,
                "role": "assistant",
                "content": text,
            }
            for text in ("a", "b")
        ])
    )

    cursors = await db.fetch_all(select(message_history_table.c.sync_cursor))
    assert len({row["sync_cursor"] for row in cursors}) == 1
    assert cursors[0]["sync_cursor"] > 0


async def test_an_update_restamps_the_row(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)
    internal_id = await _add_message(db, "c1")
    before = await _cursor_of(db, internal_id)

    await db.message_history.update_error_traceback(internal_id, "boom")

    assert await _cursor_of(db, internal_id) > before


async def test_a_delete_leaves_a_tombstone_with_the_rows_audience(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    internal_id = await _add_message(db, "c1")

    await db.execute(
        message_history_table.delete().where(
            message_history_table.c.internal_id == internal_id
        )
    )

    tombstones = await db.fetch_all(select(sync_tombstones_table))
    assert [
        (t["kind"], t["entity_id"], t["conversation_id"], t["owner_user_id"])
        for t in tombstones
    ] == [("message", str(internal_id), "c1", "alice")]


async def test_an_upsert_that_updates_restamps_the_row(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)
    now = datetime.now(UTC)
    await db.conversation_threads.mark_read(
        conversation_id="c1", viewer_user_id="alice", through_message_id=1, now=now
    )
    marker = select(conversation_read_markers_table.c.sync_cursor)
    before = await db.fetch_value(marker)

    await db.conversation_threads.mark_read(
        conversation_id="c1", viewer_user_id="alice", through_message_id=2, now=now
    )

    assert await db.fetch_value(marker) > before


async def test_a_commit_wakes_waiters_and_a_rollback_does_not(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    wakeup = sync_wakeup_for(db_engine)
    generation = wakeup.generation

    try:
        async with db.transaction() as txn:
            await txn.message_history.add_message(
                AssistantMessage(content="never"),
                interface_type="web",
                conversation_id="c1",
                timestamp=datetime.now(UTC),
            )
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    after_rollback = wakeup.generation
    await _add_message(db, "c1")

    assert after_rollback == generation
    assert wakeup.generation == generation + 1


async def test_a_later_writer_cannot_commit_below_an_open_one(
    db_engine: AsyncEngine,
) -> None:
    """The commit-order guarantee: a cursor never appears below one already visible."""
    db = Database(db_engine)
    held = asyncio.Event()
    release = asyncio.Event()

    async def first_writer() -> int:
        async with db.transaction() as txn:
            internal_id = await txn.message_history.add_message(
                AssistantMessage(content="first"),
                interface_type="web",
                conversation_id="c1",
                timestamp=datetime.now(UTC),
            )
            held.set()
            await release.wait()
        return internal_id

    first = asyncio.create_task(first_writer())
    await asyncio.wait_for(held.wait(), timeout=10)
    second = asyncio.create_task(_add_message(Database(db_engine), "c2"))
    finished_early, _ = await asyncio.wait({second}, timeout=0.5)
    release.set()
    first_id = await asyncio.wait_for(first, timeout=10)
    second_id = await asyncio.wait_for(second, timeout=10)

    assert not finished_early, (
        "the second writer committed while the first held its cursor"
    )
    assert await _cursor_of(db, second_id) > await _cursor_of(db, first_id)
