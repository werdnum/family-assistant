"""Storage table for a person's thumbs up/down rating of an assistant turn."""

from sqlalchemy import Column, DateTime, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata
from family_assistant.storage.sync_tracking import sync_cursor_column

turn_feedback_table = Table(
    "turn_feedback",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Not a foreign key: message_history has no unique turn key. Moving a
    # turn into a thread re-keys its ratings to the copy's turn id.
    Column("turn_id", String(36), nullable=False, index=True),
    Column("user_id", String(255), nullable=False),
    Column("rating", String(8), nullable=False),
    Column("comment", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint("turn_id", "user_id", name="uq_turn_feedback_turn_user"),
    sync_cursor_column(),
)
