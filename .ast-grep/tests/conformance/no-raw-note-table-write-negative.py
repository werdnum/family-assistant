"""Fixture: reads of the notes table and writes to other tables are not flagged."""

from sqlalchemy import select, update

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database, DatabaseTransaction
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.storage.tasks import tasks_table


async def read_a_title(txn: DatabaseTransaction, note_id: int) -> str | None:
    """Selecting from the table is not a write."""
    return await txn.fetch_value(
        select(notes_table.c.title).where(notes_table.c.id == note_id)
    )


async def write_another_table(txn: DatabaseTransaction, task_id: str) -> None:
    """Other tables are out of scope."""
    await txn.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id == task_id)
        .values(status="done")
    )


async def write_through_the_repository(
    db: Database, title: str, write_policy: NoteWritePolicy
) -> None:
    """The chokepoint itself."""
    await db.notes.add_or_update(
        title,
        "content",
        write_policy=write_policy,
        provenance=NoteProvenanceStamp.internal(),
    )
