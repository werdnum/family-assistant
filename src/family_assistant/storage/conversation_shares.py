"""Storage table for read-only conversation share links."""

from sqlalchemy import Column, DateTime, String, Table
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

conversation_shares_table = Table(
    "conversation_shares",
    metadata,
    Column("conversation_id", String(255), primary_key=True),
    Column("owner_user_id", String(255), nullable=False, index=True),
    Column("token_hash", String(64), nullable=False, unique=True, index=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
