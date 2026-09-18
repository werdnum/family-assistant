"""Fixture: reads that derive a policy from the active profile are not flagged."""

from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.storage.vector_search import VectorSearchQuery
from family_assistant.tools.types import ToolExecutionContext


def search_under_the_profile(exec_context: ToolExecutionContext) -> VectorSearchQuery:
    """The one correct spelling: the active profile's own confinement."""
    return VectorSearchQuery(
        search_type="keyword",
        keywords="anything",
        read_policy=exec_context.note_read_policy(),
    )


def admit_a_row_under_the_profile(
    exec_context: ToolExecutionContext, labels: list[str]
) -> bool:
    """A row-at-a-time check asks the same policy the SQL asks."""
    return exec_context.note_read_policy().admits_labels(labels)


def write_under_the_profile(exec_context: ToolExecutionContext) -> NoteWritePolicy:
    """The write side derives its own policy from the same context."""
    return exec_context.note_write_policy()
