"""
Handles storage and retrieval of notes.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

# Use absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.vector import Document  # Import Document protocol

logger = logging.getLogger(__name__)

# Define the notes table
notes_table = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("title", String, nullable=False, unique=True, index=True),
    Column("content", Text, nullable=False),
    Column("include_in_prompt", Boolean, nullable=False, server_default="true"),
    Column(
        "attachment_ids",
        Text,
        nullable=False,
        server_default="[]",
    ),  # JSON array of attachment UUIDs
    Column(
        "visibility_labels",
        Text,
        nullable=False,
        server_default="[]",
    ),  # JSON array of visibility label strings
    Column(
        "is_skill",
        Boolean,
        nullable=False,
        server_default="false",
    ),
    Column(
        "provenance_metadata_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    ),
    Column("skill_name", String, nullable=True),
    Column("skill_description", String, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    ),
)


@dataclass(frozen=True)
class NoteDocument(Document):
    """
    Represents a note document conforming to the Document protocol
    for vector storage ingestion.
    """

    _id: int | None
    _title: str
    _content: str
    _created_at: datetime
    _updated_at: datetime
    _visibility_labels: list[str] = field(default_factory=list)
    _provenance_metadata: Mapping[str, object] | None = None

    @property
    def id(self) -> int | None:
        return self._id

    @property
    def source_type(self) -> str:
        return "note"

    @property
    def source_id(self) -> str:
        return self._title  # Use title as unique identifier

    @property
    def source_uri(self) -> str | None:
        return None  # Notes don't have external URIs

    @property
    def title(self) -> str | None:
        return self._title

    @property
    def created_at(self) -> datetime | None:
        return self._created_at

    @property
    def file_path(self) -> str | None:
        return None  # Notes are text-only and don't have associated files

    @property
    # ast-grep-ignore: no-dict-any - note metadata has mixed value types for indexing
    def metadata(self) -> dict[str, Any] | None:
        metadata: dict[str, object] = {
            "title": self._title,
            "created_at": self._created_at.isoformat(),
            "updated_at": self._updated_at.isoformat(),
        }
        if self._provenance_metadata is not None:
            metadata.update(self._provenance_metadata)
        return metadata

    @property
    def visibility_labels(self) -> list[str] | None:
        return self._visibility_labels
