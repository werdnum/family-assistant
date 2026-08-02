"""The runtime invariants of commit-as-you-go database access.

These are properties of what happens at runtime rather than of any one call
site, so they are asserted here rather than reviewed by hand. See
docs/design/db-commit-as-you-go.md.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import (
    AmbientTransactionError,
    Database,
    DatabaseTransaction,
    spawn_detached,
)
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import NoteWritePolicy


async def _note_titles(db: Database) -> list[str]:
    rows = await db.fetch_all(select(notes_table.c.title))
    return [row["title"] for row in rows]


@pytest.mark.asyncio
async def test_handle_write_is_visible_to_a_separate_connection(
    db_engine: AsyncEngine,
) -> None:
    """A handle write is durable when it returns, so any other reader sees it.

    This is the #1076 bug class directly: a tool registered an attachment in
    the turn's transaction and same-turn delegation, reading on another
    connection, could not see it.
    """
    writer = Database(engine=db_engine)
    await writer.notes.add_or_update(
        title="visible-across-connections",
        content="written through the handle",
        write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
    )

    reader = Database(engine=db_engine)
    assert "visible-across-connections" in await _note_titles(reader)


@pytest.mark.postgres  # a genuinely separate engine; SQLite's StaticPool has one connection
@pytest.mark.asyncio
async def test_handle_write_is_visible_to_a_separate_engine(
    db_engine: AsyncEngine,
) -> None:
    """The same, across engines -- the strongest form of "committed"."""
    writer = Database(engine=db_engine)
    await writer.notes.add_or_update(
        title="visible-across-engines",
        content="written through the handle",
        write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
    )

    other_engine = create_engine_with_sqlite_optimizations(
        db_engine.url.render_as_string(hide_password=False)
    )
    try:
        assert "visible-across-engines" in await _note_titles(Database(other_engine))
    finally:
        await other_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_writers_on_one_handle_do_not_race(
    db_engine: AsyncEngine,
) -> None:
    """Parallel tool calls share a handle, and each gets its own transaction.

    Under the previous model they shared the turn's single AsyncConnection,
    which is not safe for concurrent use.
    """
    db = Database(engine=db_engine)

    async def write(index: int) -> None:
        await db.notes.add_or_update(
            title=f"parallel-{index}",
            content=f"written by tool {index}",
            write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
        )

    await asyncio.gather(*(write(index) for index in range(8)))

    titles = await _note_titles(Database(engine=db_engine))
    assert {f"parallel-{index}" for index in range(8)} <= set(titles)


@pytest.mark.asyncio
async def test_transaction_rolls_back_every_write_in_the_block(
    db_engine: AsyncEngine,
) -> None:
    """The point of the explicit block: all of it, or none of it."""
    db = Database(engine=db_engine)

    with pytest.raises(RuntimeError, match="deliberate"):
        async with db.transaction() as txn:
            await txn.notes.add_or_update(
                title="rolled-back",
                content="never committed",
                write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
            )
            raise RuntimeError("deliberate")

    assert "rolled-back" not in await _note_titles(Database(engine=db_engine))


@pytest.mark.asyncio
async def test_handle_use_inside_a_transaction_is_refused(
    db_engine: AsyncEngine,
) -> None:
    """The guard fires on both backends, in production, not just in tests.

    Without it this is a deadlock on SQLite and a write that escapes the
    enclosing rollback on PostgreSQL.
    """
    db = Database(engine=db_engine)

    with pytest.raises(AmbientTransactionError):
        async with db.transaction():
            await db.fetch_all(select(notes_table.c.title))


@pytest.mark.asyncio
async def test_the_guard_is_inherited_by_awaited_child_tasks(
    db_engine: AsyncEngine,
) -> None:
    """An awaited child reaching the handle is the dangerous case, not an exemption."""
    db = Database(engine=db_engine)

    async def child() -> None:
        await db.fetch_all(select(notes_table.c.title))

    with pytest.raises(AmbientTransactionError):
        async with db.transaction():
            await asyncio.gather(child())


@pytest.mark.asyncio
async def test_detached_work_opts_out_of_the_ambient_transaction(
    db_engine: AsyncEngine,
) -> None:
    """Genuinely fire-and-forget work opts out explicitly, at spawn."""
    db = Database(engine=db_engine)

    async def detached_write() -> None:
        await db.notes.add_or_update(
            title="detached",
            content="not part of the caller's transaction",
            write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
        )

    async with db.transaction() as txn:
        task = spawn_detached(detached_write())
        await txn.notes.add_or_update(
            title="enclosing",
            content="part of the caller's transaction",
            write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
        )
    # Awaited after the block: on SQLite the detached write queues on the
    # engine lock the block holds, so awaiting it inside would deadlock.
    await task

    titles = await _note_titles(Database(engine=db_engine))
    assert {"detached", "enclosing"} <= set(titles)


@pytest.mark.asyncio
async def test_on_commit_fires_once_after_the_commit_that_succeeded(
    db_engine: AsyncEngine,
) -> None:
    """Callbacks belong to the transaction and are discarded with it on rollback."""
    db = Database(engine=db_engine)
    fired: list[str] = []

    with pytest.raises(RuntimeError, match="deliberate"):
        async with db.transaction() as txn:
            txn.on_commit(lambda: fired.append("rolled-back"))
            await txn.tasks.enqueue(
                task_id="on-commit-rollback",
                task_type="quick",
                scheduled_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
            raise RuntimeError("deliberate")

    assert fired == []

    async with db.transaction() as txn:
        txn.on_commit(lambda: fired.append("committed"))

    assert fired == ["committed"]


@pytest.mark.asyncio
async def test_atomic_closure_joins_an_enclosing_transaction(
    db_engine: AsyncEngine,
) -> None:
    """A repository method's atomic() joins the caller's block rather than nesting."""
    db = Database(engine=db_engine)
    seen: list[bool] = []

    async def body(txn: DatabaseTransaction) -> None:
        seen.append(True)
        await txn.notes.add_or_update(
            title="joined",
            content="ran against the caller's transaction",
            write_policy=NoteWritePolicy.UNCONSTRAINED,  # ast-grep-ignore: no-unconstrained-note-write-policy - storage-level invariant test, not a profile write path
        )

    with pytest.raises(RuntimeError, match="deliberate"):
        async with db.transaction() as txn:
            await txn.atomic(body)
            raise RuntimeError("deliberate")

    assert seen == [True]
    assert "joined" not in await _note_titles(Database(engine=db_engine))
