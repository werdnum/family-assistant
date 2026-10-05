"""Repository for history compaction events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa

from family_assistant.storage.history_compaction import (
    HistoryScope,
    TurnDecision,
    history_compaction_events_table,
)
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class CompactionEvent:
    """A recorded event, as the loader reads it back."""

    id: int
    created_at: datetime
    boundary_internal_id: int
    active_turn_key: str | None
    reason: str
    decisions: dict[str, TurnDecision]
    changed: bool


class HistoryCompactionRepository(BaseRepository):
    """Records compaction events and reads back the latest for a window."""

    def _scope_conditions(self, scope: HistoryScope) -> list[sa.ColumnElement[bool]]:
        table = history_compaction_events_table
        return [
            table.c.interface_type == scope.interface_type,
            table.c.conversation_id == scope.conversation_id,
            table.c.processing_profile_id == scope.processing_profile_id,
            table.c.subconversation_id.is_(None)
            if scope.subconversation_id is None
            else table.c.subconversation_id == scope.subconversation_id,
        ]

    async def latest(self, scope: HistoryScope) -> CompactionEvent | None:
        """The newest event for this window, or ``None`` before the first."""
        table = history_compaction_events_table
        row = await self._db.fetch_one(
            sa
            .select(table)
            .where(*self._scope_conditions(scope))
            .order_by(table.c.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        return CompactionEvent(
            id=row["id"],
            created_at=row["created_at"],
            boundary_internal_id=row["boundary_internal_id"],
            active_turn_key=row["active_turn_key"],
            reason=row["reason"],
            decisions={
                key: TurnDecision.from_json(value)
                for key, value in (row["decisions"] or {}).items()
            },
            changed=row["changed"],
        )

    async def record(
        self,
        scope: HistoryScope,
        *,
        now: datetime,
        boundary_internal_id: int,
        active_turn_key: str | None,
        reason: str,
        decisions: Mapping[str, TurnDecision],
        changed: bool,
        details: Mapping[str, object] | None = None,
    ) -> None:
        await self._db.execute(
            sa.insert(history_compaction_events_table).values(
                created_at=now,
                interface_type=scope.interface_type,
                conversation_id=scope.conversation_id,
                processing_profile_id=scope.processing_profile_id,
                subconversation_id=scope.subconversation_id,
                boundary_internal_id=boundary_internal_id,
                active_turn_key=active_turn_key,
                reason=reason,
                decisions={key: value.to_json() for key, value in decisions.items()},
                changed=changed,
                details=dict(details) if details is not None else None,
            )
        )
