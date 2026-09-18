"""Fixture: note reads that opt out of the profile's confinement are flagged."""

from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteModel, NoteReadPolicy
from family_assistant.tools.types import ToolExecutionContext


async def read_a_note_unconfined(db: Database, title: str) -> NoteModel | None:
    """The floor that keeps a confined profile out of unlabelled notes is gone."""
    return await db.notes.get_by_title(title, read_policy=NoteReadPolicy.UNRESTRICTED)


async def list_every_note(db: Database) -> list[NoteModel]:
    """A profile-facing listing has a profile whose confinement should apply."""
    return await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)


async def hoist_the_sentinel_into_a_local(
    db: Database, exec_context: ToolExecutionContext
) -> list[NoteModel]:
    """Naming it first does not make it a different policy."""
    policy = NoteReadPolicy.UNRESTRICTED
    return await db.notes.get_prompt_notes(read_policy=policy)
