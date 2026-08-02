"""
Base module for database connection and metadata.

This module defines the SQLAlchemy engine and metadata object shared across
different storage modules to prevent circular dependencies.
"""

import logging
import os
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    event,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy.pool.base import _ConnectionRecord
from sqlalchemy.sql import func

from family_assistant.storage.instrumentation import attach_instrumentation

logger = logging.getLogger(__name__)

# Define shared metadata object
metadata = MetaData()

# Define database engine
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db")


def create_engine_with_sqlite_optimizations(
    database_url: str,
    *,
    instrument: bool = False,
) -> AsyncEngine:
    """Create engine with SQLite optimizations if applicable.

    Args:
        database_url: The database URL to connect to.
        instrument: Attach transaction-duration and connection-leak
            instrumentation (see ``storage.instrumentation``). Enabled by the
            test fixtures; off in production, where the listeners would add
            per-transaction stack capture for no benefit.
    """
    # SQLAlchemy's async engine needs an async driver. A bare "postgresql://"
    # URL (e.g. Render's connectionString, or a standard libpq URL) resolves to
    # the sync psycopg2 dialect and fails. Normalize it to asyncpg, the driver
    # this project ships.
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Determine pool class based on database type.
    #
    # SQLite uses StaticPool to funnel all access through a SINGLE DBAPI
    # connection. This is deliberate and load-bearing: file-based SQLite
    # handles concurrent writers poorly -- giving each DatabaseContext its own
    # connection produces "database is locked" errors under contention even
    # with WAL mode and a 30s busy_timeout, and in-memory databases live
    # entirely inside one connection. Serializing through a shared connection
    # avoids both. NullPool is used for PostgreSQL to avoid the "Future
    # attached to a different loop" errors with asyncpg.
    is_sqlite = database_url.startswith("sqlite")
    pool_class = StaticPool if is_sqlite else NullPool

    engine = create_async_engine(
        database_url,
        echo=False,
        connect_args={
            "timeout": 30,  # 30 second busy timeout for SQLite
            "check_same_thread": False,
        }
        if is_sqlite
        else {},
        pool_pre_ping=pool_class != NullPool,
        poolclass=pool_class,
        # The shared StaticPool connection means all SQLite contexts share one
        # transaction. The default reset-on-return issues a ROLLBACK on that
        # connection at every checkin, which aborts the shared transaction and
        # silently destroys every OTHER context's uncommitted writes (e.g. a
        # streaming turn's freshly persisted messages vanish when an unrelated
        # read-only request returns the connection). Each DatabaseContext
        # commits or rolls back its own work via engine.begin() on exit, so
        # disable the per-checkin reset for SQLite to stop the cross-context
        # clobbering.
        pool_reset_on_return=None if is_sqlite else "rollback",
    )

    # Add SQLite-specific optimizations using dialect detection
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(
        dbapi_connection: Any,  # noqa: ANN401 # DBAPI connection type varies
        connection_record: _ConnectionRecord,
    ) -> None:
        # Check if this is actually a SQLite connection
        if hasattr(dbapi_connection, "execute"):
            # Use a more robust check
            cursor = dbapi_connection.cursor()
            try:
                # This will only work on SQLite
                cursor.execute("SELECT sqlite_version()")
                cursor.fetchone()

                # If we get here, it's SQLite
                cursor.execute("PRAGMA journal_mode=WAL")  # Enable WAL mode
                cursor.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
                cursor.execute("PRAGMA synchronous=NORMAL")  # Better performance
                cursor.execute("PRAGMA cache_size=-64000")  # 64MB cache
                cursor.execute("PRAGMA temp_store=MEMORY")  # Use memory for temp tables
                cursor.execute("PRAGMA mmap_size=536870912")  # 512MB memory-mapped I/O

                logger.debug("Applied SQLite optimizations")
            except Exception:
                # Not SQLite, ignore
                pass
            finally:
                cursor.close()

    if instrument:
        attach_instrumentation(engine)

    return engine


# Global engine removed - use create_engine_with_sqlite_optimizations() directly
# Engine should be created and managed at the application level (e.g., in Assistant class)


# Define the API tokens table
api_tokens_table = Table(
    "api_tokens",
    metadata,
    Column("id", Integer, primary_key=True, index=True),
    Column(
        "user_identifier", String, nullable=False, index=True
    ),  # Identifies the user (e.g., email or an ID from an auth system)
    Column("name", String, nullable=False),  # User-friendly name for the token
    Column(
        "hashed_token", String, nullable=False, unique=True, index=True
    ),  # The securely hashed API token
    Column(
        "prefix", String(8), nullable=False, unique=True
    ),  # First 8 characters of the token for display/identification
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=func.now(),  # pylint: disable=not-callable
        nullable=False,
    ),
    Column(
        "expires_at", DateTime(timezone=True), nullable=True
    ),  # Optional expiry date
    Column(
        "last_used_at", DateTime(timezone=True), nullable=True
    ),  # Timestamp of the last usage
    Column(
        "is_revoked", Boolean, default=False, nullable=False
    ),  # Flag to indicate if the token is revoked
    Column(
        "token_type", String(16), nullable=False, server_default="api"
    ),  # "api" or "refresh"
    Column(
        "parent_token_id", Integer, ForeignKey("api_tokens.id"), nullable=True
    ),  # Links refresh token to its API token
    extend_existing=True,
)


# Define the attachment_metadata table for unified attachment tracking
attachment_metadata_table = Table(
    "attachment_metadata",
    metadata,
    Column("attachment_id", String(36), primary_key=True),  # UUID
    Column(
        "source_type", String(20), nullable=False
    ),  # "user", "tool", "script", "email"
    Column(
        "source_id", Text, nullable=False
    ),  # user_id, tool_name, script_id, email Message-Id
    Column("mime_type", String(100), nullable=False),
    Column("description", Text, nullable=True),
    Column("size", Integer, nullable=False),
    Column("content_url", Text, nullable=True),  # URL for retrieval
    Column("storage_path", Text, nullable=True),  # File system path
    # Bounded SHA-256 hex digest of ``f"{source_id}\0{storage_path}"`` used
    # to uniquely identify an email attachment regardless of how long the
    # Message-Id or path is. Only populated for ``source_type="email"``.
    Column("email_identity_hash", String(64), nullable=True),
    Column("conversation_id", String(255), nullable=True),
    Column(
        "message_id", Integer, ForeignKey("message_history.internal_id"), nullable=True
    ),
    # Canonical user id that owns this attachment. NULL means "ownerless":
    # the attachment is visible/operable by any caller (uploads, legacy,
    # non-personal tool output). An owned attachment is visible/operable only
    # when the caller's acting user matches this value.
    Column("owner_user_id", String(255), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("accessed_at", DateTime(timezone=True), nullable=True),
    Column("metadata", JSON, nullable=True),
    # Indexes for common queries
    Index("idx_attachment_conversation", "conversation_id"),
    # ``idx_attachment_source`` excludes email rows — long ``Message-Id``
    # headers stored in ``source_id`` can blow past the Postgres btree
    # index-row size limit. Email lookups are served by the partial
    # unique index on ``email_identity_hash`` below instead.
    Index(
        "idx_attachment_source",
        "source_type",
        "source_id",
        postgresql_where=text("source_type <> 'email'"),
        sqlite_where=text("source_type <> 'email'"),
    ),
    Index("idx_attachment_created", "created_at"),
    # Partial unique index: email attachments (source_type="email") must be
    # unique on the bounded ``email_identity_hash``. Indexing the raw
    # ``(source_id, storage_path)`` Text columns risks exceeding Postgres'
    # btree index-row size limit for long Message-Ids / paths.
    Index(
        "uix_attachment_metadata_email_identity",
        "email_identity_hash",
        unique=True,
        postgresql_where=text(
            "source_type = 'email' AND email_identity_hash IS NOT NULL"
        ),
        sqlite_where=text("source_type = 'email' AND email_identity_hash IS NOT NULL"),
    ),
    extend_existing=True,
)
