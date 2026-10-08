"""Deleting or renaming a note removes its search-index record."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database
from family_assistant.storage.notes import NoteDocument
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.storage.vector import (
    DocumentEmbeddingRecord,
    DocumentRecord,
    add_document,
    add_embedding,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _create_indexed_note(db: Database, title: str) -> int:
    """Create a note plus an index record and embedding, as the indexer would."""
    await db.notes.add_or_update(
        title=title,
        content="content",
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    doc_id = await add_document(
        db,
        NoteDocument(
            _id=None,
            _title=title,
            _content="content",
            _created_at=_NOW,
            _updated_at=_NOW,
        ),
    )
    await add_embedding(
        db,
        document_id=doc_id,
        chunk_index=0,
        embedding_type="content_chunk",
        embedding=None,
        embedding_model="test-model",
        content="content",
    )
    return doc_id


async def _document_exists(db: Database, doc_id: int) -> bool:
    return (
        await db.fetch_one(select(DocumentRecord.id).where(DocumentRecord.id == doc_id))
        is not None
    )


async def _embedding_count(db: Database, doc_id: int) -> int:
    rows = await db.fetch_all(
        select(DocumentEmbeddingRecord.id).where(
            DocumentEmbeddingRecord.document_id == doc_id
        )
    )
    return len(rows)


async def test_delete_removes_index_record_and_embeddings(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    doc_id = await _create_indexed_note(db, "Doomed Note")
    kept_doc_id = await _create_indexed_note(db, "Kept Note")

    assert await db.notes.delete("Doomed Note")

    assert not await _document_exists(db, doc_id)
    assert await _embedding_count(db, doc_id) == 0
    assert await _document_exists(db, kept_doc_id)
    assert await _embedding_count(db, kept_doc_id) == 1


async def test_rename_removes_index_record_under_old_title(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    doc_id = await _create_indexed_note(db, "Old Title")

    await db.notes.rename_and_update(
        "Old Title",
        "New Title",
        "content",
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    assert not await _document_exists(db, doc_id)
    assert await _embedding_count(db, doc_id) == 0


async def test_update_in_place_keeps_index_record(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    doc_id = await _create_indexed_note(db, "Stable Title")

    await db.notes.rename_and_update(
        "Stable Title",
        "Stable Title",
        "new content",
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    assert await _document_exists(db, doc_id)
