"""Repository for the memory change log.

No ``from __future__ import annotations`` here: pydantic resolves
``MemoryChangeLogEntry``'s annotations at runtime, and a deferred ``datetime``
leaves the model undefined. The type-only names below are quoted instead.
"""

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import BaseModel

from family_assistant.storage.memory_change_log import memory_change_log_table
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.memory.actor import MemoryActor
    from family_assistant.storage.memory_change_log import MemoryChangeOutcome


class MemoryChangeLogEntry(BaseModel):
    """One row of the memory change log."""

    id: int
    created_at: datetime
    batch_id: str
    actor_kind: str
    actor_identity: str | None
    interface_type: str | None
    conversation_id: str | None
    op: str | None
    note_title: str | None
    destination_note_title: str | None
    before_text: str | None
    after_text: str | None
    evidence_message_ids: list[int]
    outcome: str
    reason: str | None


class MemoryChangeLogRepository(BaseRepository):
    """Appends to, and reads back, the record of what memory learned."""

    async def add_edit(
        self,
        *,
        batch_id: str,
        actor: "MemoryActor",
        op: str,
        note_title: str,
        destination_note_title: str | None,
        before_text: str | None,
        after_text: str | None,
        evidence_message_ids: "Sequence[int]",
        now: datetime,
        reason: str | None = None,
    ) -> None:
        """Record one committed edit.

        Called from inside the apply transaction, so a list that is rejected
        and rolled back leaves nothing behind.
        """
        await self._db.execute(
            sa.insert(memory_change_log_table).values(
                created_at=now,
                batch_id=batch_id,
                actor_kind=str(actor.kind),
                actor_identity=actor.identity,
                interface_type=actor.interface_type,
                conversation_id=actor.conversation_id,
                op=op,
                note_title=note_title,
                destination_note_title=destination_note_title,
                before_text=before_text,
                after_text=after_text,
                evidence_message_ids=list(evidence_message_ids),
                outcome="applied",
                reason=reason,
            )
        )

    async def add_review_outcome(
        self,
        *,
        batch_id: str,
        actor: "MemoryActor",
        outcome: "MemoryChangeOutcome",
        reason: str,
        now: datetime,
    ) -> None:
        """Record a review that ended without edits, and why.

        A stretch skipped before the model call and a review abandoned after a
        permanent failure both advance the watermark, and both have to be
        visible in the recent-changes view or the loss is silent.
        """
        await self._db.execute(
            sa.insert(memory_change_log_table).values(
                created_at=now,
                batch_id=batch_id,
                actor_kind=str(actor.kind),
                actor_identity=actor.identity,
                interface_type=actor.interface_type,
                conversation_id=actor.conversation_id,
                op=None,
                note_title=None,
                destination_note_title=None,
                before_text=None,
                after_text=None,
                evidence_message_ids=None,
                outcome=outcome,
                reason=reason,
            )
        )

    async def get_recent(
        self,
        limit: int = 50,
        *,
        batch_id: str | None = None,
    ) -> list[MemoryChangeLogEntry]:
        """The most recent changes, newest first."""
        query = sa.select(memory_change_log_table).order_by(
            memory_change_log_table.c.created_at.desc(),
            memory_change_log_table.c.id.desc(),
        )
        if batch_id is not None:
            query = query.where(memory_change_log_table.c.batch_id == batch_id)
        rows = await self._db.fetch_all(query.limit(limit))
        return [
            MemoryChangeLogEntry(
                id=row["id"],
                created_at=row["created_at"],
                batch_id=row["batch_id"],
                actor_kind=row["actor_kind"],
                actor_identity=row["actor_identity"],
                interface_type=row["interface_type"],
                conversation_id=row["conversation_id"],
                op=row["op"],
                note_title=row["note_title"],
                destination_note_title=row["destination_note_title"],
                before_text=row["before_text"],
                after_text=row["after_text"],
                evidence_message_ids=list(row["evidence_message_ids"] or []),
                outcome=row["outcome"],
                reason=row["reason"],
            )
            for row in rows
        ]
