"""Storage tables for read-only conversation and message share links."""

from sqlalchemy import Column, DateTime, Integer, String, Table
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

# A link to one message rather than to its conversation. Keyed by the message so
# Home, which can hold many of these, has no "one active link" of its own. The
# row is matched by id with no foreign key: a turn moved to a thread is deleted
# and rewritten, and the link it held simply stops resolving.
conversation_message_shares_table = Table(
    "conversation_message_shares",
    metadata,
    Column("message_id", Integer, primary_key=True, autoincrement=False),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column("owner_user_id", String(255), nullable=False, index=True),
    Column("token_hash", String(64), nullable=False, unique=True, index=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
