"""
Base module for database connection and metadata.

This module defines the SQLAlchemy engine and metadata object shared across
different storage modules to prevent circular dependencies.
"""

import logging
import os

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
