"""Reads behind the client sync endpoint: what changed between two cursors.

Every synced row carries the cursor of the transaction that last wrote it (see
``storage/sync_tracking.py``), so each question here is a range query on
``sync_cursor``. ``after`` is exclusive and ``through`` inclusive, and
``through`` is never above :meth:`SyncRepository.current_cursor`, so a range
only ever covers transactions that have committed.
"""

from collections.abc import Collection

from sqlalchemy import ColumnElement, Table, select, union

from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.conversation_threads import (
    conversation_read_markers_table,
    conversation_threads_table,
)
from family_assistant.storage.delegation_runs import delegation_runs_table
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.sync_tracking import (
    COUNTER_ROW_ID,
    SYNC_CURSOR_COLUMN,
    sync_cursor_counter_table,
    sync_tombstones_table,
)
from family_assistant.storage.turn_feedback import turn_feedback_table


def _in_range(table: Table, after: int, through: int) -> ColumnElement[bool]:
    column = table.c[SYNC_CURSOR_COLUMN]
    return (column > after) & (column <= through)


class SyncRepository(BaseRepository):
    async def current_cursor(self) -> int:
        """The cursor of the newest committed write to any synced table."""
        value = await self._db.fetch_value(
            select(sync_cursor_counter_table.c.value).where(
                sync_cursor_counter_table.c.id == COUNTER_ROW_ID
            )
        )
        return int(value or 0)

    async def changed_conversation_ids(self, after: int, through: int) -> set[str]:
        """Conversations whose list row may read differently than at ``after``.

        A row is derived from the conversation's messages, its thread record
        and its delegation runs, so a change to any of them, including a
        deletion, touches it. Read markers are per viewer and asked separately.
        """
        statement = union(
            select(message_history_table.c.conversation_id).where(
                _in_range(message_history_table, after, through)
            ),
            select(conversation_threads_table.c.conversation_id).where(
                _in_range(conversation_threads_table, after, through)
            ),
            select(delegation_runs_table.c.conversation_id).where(
                _in_range(delegation_runs_table, after, through)
            ),
            select(sync_tombstones_table.c.conversation_id).where(
                _in_range(sync_tombstones_table, after, through),
                sync_tombstones_table.c.kind.in_(["message", "thread", "delegation"]),
                sync_tombstones_table.c.conversation_id.is_not(None),
            ),
        )
        rows = await self._db.fetch_all(select(statement.subquery()))
        return {row["conversation_id"] for row in rows}

    async def read_marker_conversation_ids(
        self, viewer_user_ids: Collection[str], after: int, through: int
    ) -> set[str]:
        """Conversations where one of the caller's read markers moved."""
        if not viewer_user_ids:
            return set()
        rows = await self._db.fetch_all(
            select(conversation_read_markers_table.c.conversation_id).where(
                _in_range(conversation_read_markers_table, after, through),
                conversation_read_markers_table.c.viewer_user_id.in_(viewer_user_ids),
            )
        )
        return {row["conversation_id"] for row in rows}

    async def delegation_conversation_ids(self, after: int, through: int) -> set[str]:
        """Conversations whose delegation runs changed, deletions included."""
        statement = union(
            select(delegation_runs_table.c.conversation_id).where(
                _in_range(delegation_runs_table, after, through)
            ),
            select(sync_tombstones_table.c.conversation_id).where(
                _in_range(sync_tombstones_table, after, through),
                sync_tombstones_table.c.kind == "delegation",
                sync_tombstones_table.c.conversation_id.is_not(None),
            ),
        )
        rows = await self._db.fetch_all(select(statement.subquery()))
        return {row["conversation_id"] for row in rows}

    async def confirmations_changed(
        self, target_user_ids: Collection[str], after: int, through: int
    ) -> bool:
        """Whether any of the caller's confirmation requests changed."""
        if not target_user_ids:
            return False
        changed = await self._db.fetch_value(
            select(confirmation_requests_table.c.id)
            .where(
                _in_range(confirmation_requests_table, after, through),
                confirmation_requests_table.c.target_user_id.in_(target_user_ids),
            )
            .limit(1)
        )
        if changed is not None:
            return True
        removed = await self._db.fetch_value(
            select(sync_tombstones_table.c.id)
            .where(
                _in_range(sync_tombstones_table, after, through),
                sync_tombstones_table.c.kind == "confirmation",
                sync_tombstones_table.c.owner_user_id.in_(target_user_ids),
            )
            .limit(1)
        )
        return removed is not None

    async def removed_message_ids(
        self, conversation_ids: Collection[str], after: int, through: int
    ) -> dict[str, list[int]]:
        """Messages deleted from ``conversation_ids`` in the range."""
        if not conversation_ids:
            return {}
        rows = await self._db.fetch_all(
            select(
                sync_tombstones_table.c.conversation_id,
                sync_tombstones_table.c.entity_id,
            )
            .where(
                _in_range(sync_tombstones_table, after, through),
                sync_tombstones_table.c.kind == "message",
                sync_tombstones_table.c.conversation_id.in_(conversation_ids),
            )
            .order_by(sync_tombstones_table.c.id)
        )
        removed: dict[str, list[int]] = {}
        for row in rows:
            removed.setdefault(row["conversation_id"], []).append(int(row["entity_id"]))
        return removed

    async def removed_conversation_ids(
        self, owner_user_ids: Collection[str], after: int, through: int
    ) -> set[str]:
        """Conversations of the caller's that had messages deleted in the range.

        Ownership of a deleted row cannot be asked of the row, so this uses the
        owner the tombstone recorded. The caller keeps only those that no
        longer have a list row at all.
        """
        if not owner_user_ids:
            return set()
        rows = await self._db.fetch_all(
            select(sync_tombstones_table.c.conversation_id)
            .where(
                _in_range(sync_tombstones_table, after, through),
                sync_tombstones_table.c.kind == "message",
                sync_tombstones_table.c.owner_user_id.in_(owner_user_ids),
                sync_tombstones_table.c.conversation_id.is_not(None),
            )
            .distinct()
        )
        return {row["conversation_id"] for row in rows}

    async def feedback_turn_ids(
        self, user_ids: Collection[str], after: int, through: int
    ) -> set[str]:
        """Turns whose rating by one of ``user_ids`` was set or cleared."""
        if not user_ids:
            return set()
        statement = union(
            select(turn_feedback_table.c.turn_id.label("turn_id")).where(
                _in_range(turn_feedback_table, after, through),
                turn_feedback_table.c.user_id.in_(user_ids),
            ),
            select(sync_tombstones_table.c.entity_id.label("turn_id")).where(
                _in_range(sync_tombstones_table, after, through),
                sync_tombstones_table.c.kind == "feedback",
                sync_tombstones_table.c.owner_user_id.in_(user_ids),
            ),
        )
        rows = await self._db.fetch_all(select(statement.subquery()))
        return {row["turn_id"] for row in rows}
