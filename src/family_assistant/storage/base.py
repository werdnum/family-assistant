"""
Base module for database connection and metadata.

This module defines the SQLAlchemy engine and metadata object shared across
different storage modules to prevent circular dependencies.
"""

import logging
import os
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    event,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)

# Define shared metadata object
metadata = MetaData()

# Define database engine
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db")


def create_engine_with_sqlite_optimizations(database_url: str) -> AsyncEngine:
    """Create engine with SQLite optimizations if applicable."""
    # Determine pool class based on database type
    # Use StaticPool for SQLite to reuse connections
    # Use NullPool for PostgreSQL to avoid event loop affinity issues
    # NullPool creates a new connection for each request, which is less efficient
    # but avoids the "Future attached to a different loop" errors with asyncpg
    pool_class = StaticPool if database_url.startswith("sqlite") else NullPool

    # Create the engine first
    engine = create_async_engine(
        database_url,
        echo=False,
        connect_args={
            "timeout": 30,  # 30 second busy timeout for SQLite
            "check_same_thread": False,
        }
        if database_url.startswith("sqlite")
        else {},
        pool_pre_ping=pool_class != NullPool,
        poolclass=pool_class,
    )

    # Add SQLite-specific optimizations using dialect detection
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
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

    return engine


engine = create_engine_with_sqlite_optimizations(DATABASE_URL)
pool_info = (
    "StaticPool"
    if DATABASE_URL.startswith("sqlite")
    else "NullPool (no connection reuse)"
)
logger.info(
    f"SQLAlchemy engine created for URL: {DATABASE_URL.split('@')[-1]} with {pool_info}"
)  # Log URL safely


def get_engine() -> AsyncEngine:
    """Returns the initialized SQLAlchemy async engine."""
    return engine


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
    extend_existing=True,
)

logger.info("Defined api_tokens table schema.")
