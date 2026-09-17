"""Repository for the memory review watermarks and the enablement boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from family_assistant.storage.memory_review import (
    memory_contribution_state_table,
    memory_review_watermarks_table,
)
from family_assistant.storage.repositories.base import BaseRepository


@dataclass(frozen=True)
class ReviewWatermark:
    """How far a conversation has been reviewed."""

    interface_type: str
    conversation_id: str
    last_reviewed_internal_id: int
    last_reviewed_at: datetime | None


class MemoryReviewRepository(BaseRepository):
    """Reads and writes the state the due predicate is evaluated against.

    Both writes are idempotent upserts rather than read-modify-write, because
    the watermark advance runs inside the apply transaction (see
    ``apply_memory_edits_atomically``'s ``after_apply`` hook) where a second
    round trip to decide what to write would widen a transaction the design
    requires to stay short.
    """

    async def get_watermark(
        self, *, interface_type: str, conversation_id: str
    ) -> ReviewWatermark | None:
        """The conversation's watermark, or None if it has never been reviewed."""
        row = await self._db.fetch_one(
            sa.select(memory_review_watermarks_table).where(
                memory_review_watermarks_table.c.interface_type == interface_type,
                memory_review_watermarks_table.c.conversation_id == conversation_id,
            )
        )
        if row is None:
            return None
        return ReviewWatermark(
            interface_type=row["interface_type"],
            conversation_id=row["conversation_id"],
            last_reviewed_internal_id=int(row["last_reviewed_internal_id"]),
            last_reviewed_at=row["last_reviewed_at"],
        )

    async def advance_watermark(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        last_reviewed_internal_id: int,
        now: datetime,
    ) -> None:
        """Move the watermark forward, never backwards.

        Monotonic in the database rather than in the caller: a review that was
        retried, or one that raced a later review of the same conversation,
        must not be able to re-expose a stretch that has already been curated.
        """
        moment = now.astimezone(UTC)
        values = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "last_reviewed_internal_id": last_reviewed_internal_id,
            "last_reviewed_at": moment,
            "updated_at": moment,
        }
        updates = {
            "last_reviewed_internal_id": last_reviewed_internal_id,
            "last_reviewed_at": moment,
            "updated_at": moment,
        }
        moves_forward = (
            memory_review_watermarks_table.c.last_reviewed_internal_id
            < last_reviewed_internal_id
        )
        if self._db.dialect_name == "postgresql":
            statement = pg_insert(memory_review_watermarks_table).values(**values)
            await self._db.execute(
                statement.on_conflict_do_update(
                    index_elements=["interface_type", "conversation_id"],
                    set_=updates,
                    where=moves_forward,
                )
            )
            return
        sqlite_statement = sqlite_insert(memory_review_watermarks_table).values(
            **values
        )
        await self._db.execute(
            sqlite_statement.on_conflict_do_update(
                index_elements=["interface_type", "conversation_id"],
                set_=updates,
                where=moves_forward,
            )
        )

    async def get_enablement(self) -> dict[str, datetime]:
        """Each currently-contributing profile and the moment it was turned on."""
        rows = await self._db.fetch_all(
            sa.select(
                memory_contribution_state_table.c.profile_id,
                memory_contribution_state_table.c.enabled_at,
            ).where(memory_contribution_state_table.c.enabled.is_(True))
        )
        return {
            row["profile_id"]: _as_utc(row["enabled_at"])
            for row in rows
            if row["enabled_at"] is not None
        }

    async def record_enablement(
        self, *, profile_ids_contributing: set[str], now: datetime
    ) -> None:
        """Reconcile the stored enablement with what the configuration says.

        A profile that is configured to contribute and is not already recorded
        as doing so gets a fresh moment; one that is already on keeps the
        moment it has, so a restart does not silently re-stamp the boundary and
        discard everything said since. A profile that has stopped contributing
        is disabled, which discards whatever of its conversations had not been
        reviewed -- one boundary per enablement is what keeps eligibility a
        single comparison.
        """
        moment = now.astimezone(UTC)
        stored = await self._db.fetch_all(
            sa.select(
                memory_contribution_state_table.c.profile_id,
                memory_contribution_state_table.c.enabled,
            )
        )
        already_on = {row["profile_id"] for row in stored if row["enabled"]}

        for profile_id in sorted(profile_ids_contributing - already_on):
            await self._upsert_enablement(
                profile_id=profile_id, enabled=True, enabled_at=moment, now=moment
            )
        for profile_id in sorted(already_on - profile_ids_contributing):
            await self._db.execute(
                sa
                .update(memory_contribution_state_table)
                .where(memory_contribution_state_table.c.profile_id == profile_id)
                .values(enabled=False, updated_at=moment)
            )

    async def _upsert_enablement(
        self, *, profile_id: str, enabled: bool, enabled_at: datetime, now: datetime
    ) -> None:
        """Insert a profile's enablement row, or turn an existing one back on."""
        values = {
            "profile_id": profile_id,
            "enabled": enabled,
            "enabled_at": enabled_at,
            "updated_at": now,
        }
        updates = {"enabled": enabled, "enabled_at": enabled_at, "updated_at": now}
        if self._db.dialect_name == "postgresql":
            statement = pg_insert(memory_contribution_state_table).values(**values)
            await self._db.execute(
                statement.on_conflict_do_update(
                    index_elements=["profile_id"], set_=updates
                )
            )
            return
        sqlite_statement = sqlite_insert(memory_contribution_state_table).values(
            **values
        )
        await self._db.execute(
            sqlite_statement.on_conflict_do_update(
                index_elements=["profile_id"], set_=updates
            )
        )


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp, as SQLite hands them back."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
