"""Durable provenance for events written through the assistant."""

from datetime import UTC, datetime
from typing import TypedDict

from sqlalchemy import JSON, Column, DateTime, String, Table

from family_assistant.storage.base import metadata


class CalendarProvenanceRecord(TypedDict):
    """The stored stamp required to admit an assistant-written event."""

    marker: str
    owner_user_id: str | None
    source_key: str
    event_uid: str
    event_version: str
    taint_metadata: object


calendar_event_provenance_table = Table(
    "calendar_event_provenance",
    metadata,
    Column("marker", String(36), primary_key=True),
    Column("owner_user_id", String(255), nullable=True),
    Column("source_key", String(1024), nullable=False),
    Column("event_uid", String(1024), nullable=False),
    Column("event_version", String(255), nullable=False),
    Column("taint_metadata", JSON, nullable=False),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    ),
)
