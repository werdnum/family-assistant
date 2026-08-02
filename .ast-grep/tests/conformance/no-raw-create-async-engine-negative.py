"""Fixture: engines built through the project factory must not be flagged."""

from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.base import create_engine_with_sqlite_optimizations


def build_engine(url: str) -> AsyncEngine:
    return create_engine_with_sqlite_optimizations(url)


def build_instrumented_engine(url: str) -> AsyncEngine:
    return create_engine_with_sqlite_optimizations(url, instrument=True)
