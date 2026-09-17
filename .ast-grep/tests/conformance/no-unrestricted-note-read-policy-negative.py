"""Fixture: note reads that derive the policy from the active profile are not flagged."""

from family_assistant.skills.types import ParsedSkill
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteModel, NoteWritePolicy
from family_assistant.tools.types import ToolExecutionContext


async def read_a_note_under_the_profile(
    exec_context: ToolExecutionContext, title: str
) -> NoteModel | None:
    """The one correct spelling: the active profile's own confinement."""
    return await exec_context.db_context.notes.get_by_title(
        title, read_policy=exec_context.note_read_policy()
    )


async def list_notes_under_the_profile(
    exec_context: ToolExecutionContext,
) -> list[NoteModel]:
    """A listing is confined the same way a single lookup is."""
    return await exec_context.db_context.notes.get_all(
        read_policy=exec_context.note_read_policy()
    )


def resolve_a_file_skill(
    exec_context: ToolExecutionContext, name: str
) -> ParsedSkill | None:
    """The skill registry takes the same policy object the repository takes."""
    assert exec_context.note_registry is not None
    return exec_context.note_registry.get_skill_by_name(
        name, exec_context.note_read_policy()
    )


async def see_before_overwrite(
    db: Database, title: str, write_policy: NoteWritePolicy
) -> NoteModel | None:
    """The write policy derives its own read; it is not an opt-out."""
    return await db.notes.get_by_title(
        title, read_policy=write_policy.see_before_overwrite_read_policy()
    )
