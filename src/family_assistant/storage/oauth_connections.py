"""Storage tables for per-user OAuth connections and pending flows.

``user_oauth_connections`` holds one active connection per (user, provider) with
the Fernet-encrypted refresh token. ``pending_oauth_flows`` holds
short-lived, single-use OAuth authorization-code flows (hashed state nonce, PKCE
verifier) that the callback atomically claims.
"""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata

user_oauth_connections_table = Table(
    "user_oauth_connections",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(255), nullable=False),
    Column("provider", String(64), nullable=False, server_default="google"),
    Column("provider_account_email", String(255), nullable=False),
    Column(
        "scopes",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=False,
    ),
    Column("refresh_token_encrypted", Text, nullable=False),
    Column("credential_generation", String(36), nullable=False),
    Column("status", String(32), nullable=False, server_default="active"),
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
    UniqueConstraint(
        "user_id", "provider", name="uq_user_oauth_connections_user_provider"
    ),
)

pending_oauth_flows_table = Table(
    "pending_oauth_flows",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("state_hash", String(64), nullable=False, unique=True),
    Column("code_verifier", String(128), nullable=False),
    Column("user_id", String(255), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Index("ix_pending_oauth_flows_created_at", "created_at"),
)
