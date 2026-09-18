"""Repository for the single-row household memory store."""

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from family_assistant.memory.invariants import MemoryStoreRevisionConflict
from family_assistant.storage.memory_store import MEMORY_STORE_ID, memory_store_table
from family_assistant.storage.repositories.base import BaseRepository


class MemoryStoreRepository(BaseRepository):
    """Reads and writes the household memory store's revision and core note.

    The row is created lazily on first use rather than seeded by the migration,
    because test schemas are built from the metadata with ``create_all`` and
    would otherwise never have one. Creation is an idempotent
    insert-or-do-nothing on both backends, so two concurrent first writes do
    not race.
    """

    async def ensure_row(self) -> None:
        """Create the store row if it does not exist yet."""
        values = {
            "id": MEMORY_STORE_ID,
            "revision": 0,
            "core_note_id": None,
            "updated_at": datetime.now(UTC),
        }
        if self._db.dialect_name == "postgresql":
            stmt = pg_insert(memory_store_table).values(**values)
            await self._db.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
            return
        sqlite_stmt = sqlite_insert(memory_store_table).values(**values)
        await self._db.execute(
            sqlite_stmt.on_conflict_do_nothing(index_elements=["id"])
        )

    async def get_revision(self) -> int:
        """The current household store revision (0 before the first write)."""
        await self.ensure_row()
        revision = await self._db.fetch_value(
            sa.select(memory_store_table.c.revision).where(
                memory_store_table.c.id == MEMORY_STORE_ID
            )
        )
        return int(revision)

    async def bump_revision(self, *, expected_revision: int | None = None) -> int:
        """Increment the store revision, optionally conditional on its value.

        With ``expected_revision`` set, the bump is the compare-and-set that
        makes a proposal computed against an older store fail rather than
        overwrite what a person changed in the meantime. Without it — the
        whole-note writes the notes UI and the foreground tool make — the bump
        is unconditional.

        Returns:
            The new revision.

        Raises:
            MemoryStoreRevisionConflict: when ``expected_revision`` is set and
                the store has moved since it was read.
        """
        await self.ensure_row()
        stmt = (
            sa
            .update(memory_store_table)
            .where(memory_store_table.c.id == MEMORY_STORE_ID)
            .values(
                revision=memory_store_table.c.revision + 1,
                updated_at=datetime.now(UTC),
            )
        )
        if expected_revision is not None:
            stmt = stmt.where(memory_store_table.c.revision == expected_revision)
        result = await self._db.execute(stmt)
        if result.rowcount != 1:
            raise MemoryStoreRevisionConflict(
                "Memory was changed while this edit was being prepared "
                f"(it expected store revision {expected_revision}). Re-read the "
                "memory notes and propose the edit against what is there now."
                if expected_revision is not None
                else "The household memory store row is missing; the memory "
                "write was refused."
            )
        return await self.get_revision()

    async def get_core_note_id(self) -> int | None:
        """The id of the always-loaded core memory note, if one exists."""
        await self.ensure_row()
        return await self._db.fetch_value(
            sa.select(memory_store_table.c.core_note_id).where(
                memory_store_table.c.id == MEMORY_STORE_ID
            )
        )

    async def set_core_note_id(self, note_id: int) -> None:
        """Record which note is the always-loaded core memory note."""
        await self.ensure_row()
        await self._db.execute(
            sa
            .update(memory_store_table)
            .where(memory_store_table.c.id == MEMORY_STORE_ID)
            .values(core_note_id=note_id, updated_at=datetime.now(UTC))
        )
