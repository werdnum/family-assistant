"""Storage for the deployment's ChatGPT plan sign-in.

One row at most: the deployment runs on a single household ChatGPT plan (see
``docs/design/chatgpt-plan-usage.md``). Tokens are Fernet ciphertext; the
registration identity (issued client id, subject, email, host id) outlives a
sign-out so a later sign-in can reuse it.
"""

from sqlalchemy import JSON, Column, DateTime, Integer, String, Table, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

chatgpt_plan_credentials_table = Table(
    "chatgpt_plan_credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ext_agent_host_id", String(128), nullable=False),
    Column("issued_client_id", String(255), nullable=False),
    Column("subject", String(255), nullable=False),
    Column("email", String(255), nullable=True),
    Column(
        "scopes",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=False,
    ),
    Column("id_token_encrypted", Text, nullable=True),
    Column("access_token_encrypted", Text, nullable=True),
    Column("refresh_token_encrypted", Text, nullable=True),
    Column("access_token_expires_at", DateTime(timezone=True), nullable=True),
    Column("earliest_refresh_at", DateTime(timezone=True), nullable=True),
    Column("status", String(32), nullable=False),
    Column("credential_generation", String(36), nullable=False),
    Column("connected_by_user_id", String(255), nullable=True),
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
    Column("last_used_at", DateTime(timezone=True), nullable=True),
)
