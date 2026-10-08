"""Data migration: delete index records whose note no longer exists.

The migration-built SQLite schema has no embeddings table (it needs pgvector),
so this covers the document records only.
"""

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, insert, select

from alembic import command
from family_assistant.storage.notes import notes_table
from family_assistant.storage.vector import DocumentRecord

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
_PRIOR_HEAD = "add_taint_audit_previous_tier"
_CLEANUP_HEAD = "delete_orphaned_note_documents"
# The migration-built SQLite schema carries ``now()`` server defaults that
# SQLite cannot evaluate, so rows set their timestamps explicitly.
_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def test_deletes_only_note_documents_without_a_note(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'orphans.db'}")
    try:
        config = Config(str(_ALEMBIC_INI))
        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _PRIOR_HEAD)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            conn.execute(
                notes_table.insert(),
                [
                    {
                        "title": "Live Note",
                        "content": "content",
                        "created_at": _NOW,
                        "updated_at": _NOW,
                    }
                ],
            )
            conn.execute(
                insert(DocumentRecord),
                [
                    {"id": 1, "source_type": "note", "source_id": "Live Note"},
                    {"id": 2, "source_type": "note", "source_id": "Deleted Note"},
                    {"id": 3, "source_type": "email", "source_id": "msg-1"},
                ],
            )

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, _CLEANUP_HEAD)

        with engine.connect() as conn:
            document_ids = set(conn.execute(select(DocumentRecord.id)).scalars())
        assert document_ids == {1, 3}
    finally:
        engine.dispose()
