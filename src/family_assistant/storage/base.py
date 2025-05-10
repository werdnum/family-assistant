"""
Base module for database connection and metadata.

This module defines the SQLAlchemy engine and metadata object shared across
different storage modules to prevent circular dependencies.
"""

import logging
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)

# Define shared metadata object
metadata = MetaData()

# Define database engine
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db")
engine = create_async_engine(DATABASE_URL, echo=False)
logger.info(
    f"SQLAlchemy engine created for URL: {DATABASE_URL.split('@')[-1]}"
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
        server_default=func.now(),
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
