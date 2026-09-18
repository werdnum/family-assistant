"""The single-row household memory store.

Holds the two pieces of state that are about the memory store as a whole
rather than about any one memory note: the revision every memory write bumps,
and the identity of the always-loaded core note.

The core note is identified by id rather than by title convention, so it
survives a rename (see docs/design/conversation-memory.md).
"""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Table,
)

from family_assistant.storage.base import metadata

MEMORY_STORE_ID = 1

memory_store_table = Table(
    "memory_store",
    metadata,
    # A household has exactly one memory store. The check constraint is what
    # makes "the row" a statement the database holds rather than a convention
    # every query has to remember.
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("revision", BigInteger, nullable=False, server_default="0"),
    # ON DELETE SET NULL rather than RESTRICT: the repository already refuses
    # to delete the core note, and a row removed out of band (a test truncating
    # notes) should leave the store bootstrappable rather than dangling.
    Column(
        "core_note_id",
        Integer,
        ForeignKey("notes.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    ),
    CheckConstraint(f"id = {MEMORY_STORE_ID}", name="ck_memory_store_singleton"),
)
