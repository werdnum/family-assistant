"""Handles storage of reusable automation scripts."""

import logging
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Integer, String, Table, Text

from family_assistant.storage.base import metadata

logger = logging.getLogger(__name__)

scripts_table = Table(
    "scripts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False, unique=True, index=True),
    Column("description", Text, nullable=False),
    Column("script_code", Text, nullable=False),
    Column(
        "parameters_schema", Text, nullable=True
    ),  # JSON Schema for expected parameters
    # Definition record for the script body: authoring stamp, content hash, and
    # creation disposition, as JSON text. See
    # docs/design/executable-definition-taint.md.
    Column("definition_record", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(UTC)),
    Column(
        "updated_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    ),
)
