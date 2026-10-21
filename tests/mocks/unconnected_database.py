"""A real ``Database`` handle for code paths that hold one but never query it.

Many tool and context objects require a ``Database`` even when the behaviour
under test touches no data. This handle is real, over an in-memory SQLite
engine that opens no connection until first used, so a test that unexpectedly
queries it fails on the missing schema instead of a mock silently answering.
"""

from __future__ import annotations

from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.database import Database


def unconnected_database() -> Database:
    """A ``Database`` whose engine is never expected to connect."""
    return Database(create_engine_with_sqlite_optimizations("sqlite+aiosqlite://"))
