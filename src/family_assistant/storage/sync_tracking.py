"""Commit-ordered change tracking for client sync.

Clients keep a local copy of their conversations, messages, threads, pending
confirmations and delegations, and ask "what changed since cursor N". Every row
of a synced table carries the ``sync_cursor`` of the transaction that last wrote
it, so the answer is a query of the state itself, already compacted to one entry
per row. A deleted row leaves a tombstone, because it cannot answer the query
any more.

Stamping happens in :class:`~family_assistant.storage.database.DatabaseTransaction`
rather than in each writer: every symbolic write to a synced table is stamped,
so a write path added later cannot forget to. See
docs/design/mobile-state-sync.md, "Change tracking".

The cursor comes from one counter row that the writing transaction updates and
holds until it commits. Writers to synced tables therefore commit in cursor
order, which is what makes "every row above my cursor is visible to me" true; a
database sequence would hand out numbers outside the transaction and let a
lower one become visible after a client had moved past it.
"""

import asyncio
import weakref
from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    DateTime,
    Integer,
    String,
    Table,
    event,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.base import metadata

SYNC_CURSOR_COLUMN = "sync_cursor"
COUNTER_ROW_ID = 1

sync_cursor_counter_table = Table(
    "sync_cursor_counter",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("value", BigInteger, nullable=False),
)


@event.listens_for(sync_cursor_counter_table, "after_create")
def _seed_counter(target: Table, connection: Connection, **_kw: object) -> None:
    """Give a freshly created schema its single counter row.

    The migration seeds it too; this covers schemas built straight from the
    metadata, as tests build theirs.
    """
    connection.execute(target.insert().values(id=COUNTER_ROW_ID, value=0))


sync_tombstones_table = Table(
    "sync_tombstones",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(SYNC_CURSOR_COLUMN, BigInteger, nullable=False, index=True),
    Column("kind", String(32), nullable=False),
    Column("entity_id", String(255), nullable=False),
    # The deleted row's audience, recorded because the row can no longer be
    # asked: its conversation, and the user it belonged to.
    Column("conversation_id", String(255), nullable=True),
    Column("owner_user_id", String(255), nullable=True),
    Column("deleted_at", DateTime(timezone=True), nullable=False),
)


def sync_cursor_column() -> Column[int]:
    """The ``sync_cursor`` column every synced table carries.

    Zero for rows written before tracking existed; a client bootstrapping from
    cursor zero receives them through the bootstrap snapshot instead.
    """
    return Column(
        SYNC_CURSOR_COLUMN, BigInteger, nullable=False, server_default="0", index=True
    )


@dataclass(frozen=True)
class SyncedTable:
    """How a synced table's rows map onto the client's collections.

    ``kind`` names the collection. ``entity_column`` identifies a row to the
    client. A deleted row's tombstone records ``conversation_column`` and
    ``owner_column`` so it can be delivered to the same audience the row had.
    """

    kind: str
    entity_column: str
    conversation_column: str | None
    owner_column: str | None


SYNCED_TABLES: dict[str, SyncedTable] = {
    "message_history": SyncedTable(
        kind="message",
        entity_column="internal_id",
        conversation_column="conversation_id",
        owner_column="user_id",
    ),
    "conversation_threads": SyncedTable(
        kind="thread",
        entity_column="conversation_id",
        conversation_column="conversation_id",
        owner_column="owner_user_id",
    ),
    "conversation_read_markers": SyncedTable(
        kind="read_marker",
        entity_column="conversation_id",
        conversation_column="conversation_id",
        owner_column="viewer_user_id",
    ),
    "confirmation_requests": SyncedTable(
        kind="confirmation",
        entity_column="id",
        conversation_column="origin_conversation_id",
        owner_column="target_user_id",
    ),
    "delegation_runs": SyncedTable(
        kind="delegation",
        entity_column="delegation_id",
        conversation_column="conversation_id",
        owner_column="user_id",
    ),
    # A rating is shown on the messages of its turn, so a change resends them.
    "turn_feedback": SyncedTable(
        kind="feedback",
        entity_column="turn_id",
        conversation_column=None,
        owner_column="user_id",
    ),
}


def synced_table(table_name: str) -> SyncedTable | None:
    return SYNCED_TABLES.get(table_name)


class SyncWakeup:
    """Wakes everyone waiting for a synced write to commit.

    One per engine. A waiter takes the current generation, then waits for it to
    change, so a commit that lands between the waiter's read of the database
    and its wait is not missed.
    """

    def __init__(self) -> None:
        self._generation = 0
        self._changed = asyncio.Event()

    @property
    def generation(self) -> int:
        return self._generation

    def notify(self) -> None:
        self._generation += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def wait_beyond(self, generation: int, timeout: float) -> bool:
        """Wait until a commit after ``generation``; False if ``timeout`` passed."""
        if self._generation != generation:
            return True
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except TimeoutError:
            return self._generation != generation
        return True


_ENGINE_SYNC_WAKEUPS: weakref.WeakKeyDictionary[AsyncEngine, SyncWakeup] = (
    weakref.WeakKeyDictionary()
)


def sync_wakeup_for(engine: AsyncEngine) -> SyncWakeup:
    wakeup = _ENGINE_SYNC_WAKEUPS.get(engine)
    if wakeup is None:
        wakeup = SyncWakeup()
        _ENGINE_SYNC_WAKEUPS[engine] = wakeup
    return wakeup
