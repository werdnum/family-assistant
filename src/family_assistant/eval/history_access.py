"""Read-only access to a message-history database for offline eval tooling.

Every offline extraction reads a database that is expected to be the real
one, or an archival copy kept deliberately at its original revision, so none
of them may write to it. Two rules follow, and this module is their one
definition so the extraction scripts cannot drift from each other.

The schema is never initialised. The application's ``init_db`` migrates a
managed database and creates and stamps one that is not, so calling it from a
tool pointed at production would migrate a live deployment. A database whose
schema predates what a tool reads fails on the query instead, which is the
right outcome for a tool pointed at the wrong place.

The connection is read-only where the driver can enforce it. A file-backed
SQLite URL is rewritten to ``mode=ro``, so the driver refuses every write the
tool could attempt, including one added later by someone who did not read
this. The engine's tuning pragmas are off because the connect hook issues
``PRAGMA journal_mode=WAL``, a persistent property of the file that would
convert an archival copy and leave sidecar files beside it. PostgreSQL grants
read-only access through its own role system rather than the URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url

from family_assistant.storage.base import create_engine_with_sqlite_optimizations

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["open_read_only_engine", "read_only_database_url"]


def read_only_database_url(database_url: str) -> str:
    """Rewrite a file-backed SQLite URL to open read-only.

    The URL is parsed and rebuilt rather than concatenated. Appending
    ``?mode=ro&uri=true`` to a URL that already carries parameters produces a
    second ``?``, which SQLAlchemy folds into the preceding value
    (``timeout=5?mode=ro``) and then sees no ``mode`` at all -- the protection
    silently absent exactly where a URL was most deliberately written.

    Only file-backed SQLite is rewritten. ``:memory:`` has nothing to protect,
    a URL already in URI form is left as the caller wrote it, and any other
    dialect is returned unchanged.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return database_url
    database = url.database
    if not database or database.startswith("file:") or database == ":memory:":
        return database_url
    return str(
        url.set(database=f"file:{database}").update_query_dict({
            "mode": "ro",
            "uri": "true",
        })
    )


def open_read_only_engine(database_url: str) -> AsyncEngine:
    """Return an engine on ``database_url`` that cannot write a SQLite file."""
    return create_engine_with_sqlite_optimizations(
        read_only_database_url(database_url), apply_sqlite_pragmas=False
    )
