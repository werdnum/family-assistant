"""The audit trail behind the recent-changes view.

See docs/design/conversation-memory.md, "User visibility and control": a person
must be able to see what the notebook learned, with each change's before-and-
after text and its evidence, and fix it in the notes UI. The rows are written
inside the apply transaction, so a list that was rejected and rolled back
leaves none; a review that ended without edits (skipped for taint, or abandoned
after a permanent failure) records its outcome and reason with no edit
attached, which is why every edit-shaped column is nullable.
"""

from typing import Literal

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from family_assistant.storage.base import metadata

MemoryChangeOutcome = Literal["applied", "skipped", "abandoned", "no_changes"]
"""What became of the work this row records.

``applied`` is one committed edit. The other three are review outcomes with no
edit: a stretch that was not reviewed (``skipped``), a review given up on after
its watermark was advanced past it (``abandoned``), and a review that read the
stretch and found nothing durable in it (``no_changes``). The last is kept
distinct from ``applied`` with no rows, because "the curator looked and decided
nothing was worth keeping" is the common case and a person reading the
recent-changes view should be able to tell it from a review that failed.
"""

MemoryActorKindValue = Literal["curator", "assistant", "person"]

memory_change_log_table = Table(
    "memory_change_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Groups the edits of one apply, so the view can show a review as the unit
    # a person reasons about rather than as loose rows.
    Column("batch_id", String(36), nullable=False),
    Column("actor_kind", String(32), nullable=False),
    Column("actor_identity", String(255), nullable=True),
    Column("interface_type", String(50), nullable=True),
    Column("conversation_id", String(255), nullable=True),
    Column("op", String(16), nullable=True),
    Column("note_title", String(255), nullable=True),
    Column("destination_note_title", String(255), nullable=True),
    Column("before_text", Text, nullable=True),
    Column("after_text", Text, nullable=True),
    Column(
        "evidence_message_ids",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("outcome", String(16), nullable=False),
    Column("reason", Text, nullable=True),
    Index("ix_memory_change_log_created_at", "created_at"),
    Index("ix_memory_change_log_batch_id", "batch_id"),
)
