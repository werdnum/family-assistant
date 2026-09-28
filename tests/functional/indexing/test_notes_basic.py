"""
End-to-end functional test for the notes indexing pipeline.
Tests the complete flow: note creation -> automatic indexing -> vector search.
"""

import asyncio
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import EmbeddingResult
from family_assistant.indexing.notes_indexer import NotesIndexer
from family_assistant.indexing.pipeline import ContentProcessor, IndexingPipeline
from family_assistant.indexing.processors import EmbeddingDispatchProcessor
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.storage.vector import query_vectors
from family_assistant.task_worker import TaskWorker
from tests.helpers import wait_for_tasks_to_complete

TEST_EMBEDDING_MODEL = "mock-notes-model"
TEST_EMBEDDING_DIMENSION = 128

TEST_NOTE_TITLE = "Quantum Computing Research"
TEST_NOTE_CONTENT = """
Quantum computing leverages quantum mechanical phenomena such as superposition and entanglement to process information.

Key concepts include:
- Qubits: The basic unit of quantum information
- Superposition: Qubits can exist in multiple states simultaneously
- Entanglement: Quantum particles become correlated in ways that classical physics cannot explain

Applications in cryptography and optimization are particularly promising for the next decade.
"""

DISTRACTOR_NOTE_TITLE = "Sourdough Starter Care"
DISTRACTOR_NOTE_CONTENT = """
Feed the sourdough starter daily with equal weights of flour and water.
Keep it at room temperature and discard half before each feeding.
"""

TEST_QUERY_KEYWORD = "superposition qubits"


def _vector(x: float, y: float) -> list[float]:
    return [x, y] + [0.0] * (TEST_EMBEDDING_DIMENSION - 2)


QUANTUM_NOTE_VECTOR = _vector(1.0, 0.0)
SOURDOUGH_NOTE_VECTOR = _vector(0.0, 1.0)
SEMANTIC_QUERY_VECTOR = _vector(1.0, 0.1)
# Leans towards the sourdough note, so only the keyword match can rank the
# quantum note first.
KEYWORD_QUERY_VECTOR = _vector(0.1, 1.0)

TOPIC_VECTORS = {
    "quantum": QUANTUM_NOTE_VECTOR,
    "sourdough": SOURDOUGH_NOTE_VECTOR,
}


class _TopicEmbeddingGenerator:
    """Embeds each text as the vector of the one topic word it mentions."""

    @property
    def model_name(self) -> str:
        return TEST_EMBEDDING_MODEL

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[self._embed(text) for text in texts],
            model_name=self.model_name,
        )

    @staticmethod
    def _embed(text: str) -> list[float]:
        matches = [
            vector for word, vector in TOPIC_VECTORS.items() if word in text.lower()
        ]
        if len(matches) != 1:
            raise LookupError(f"Expected exactly one topic word in {text!r}")
        return matches[0]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_indexing_e2e(pg_vector_db_engine: AsyncEngine) -> None:
    """Saving notes indexes them so vector and hybrid search rank the right note first."""
    processors: list[ContentProcessor] = [
        EmbeddingDispatchProcessor(embedding_types_to_dispatch=["raw_note_text"])
    ]
    notes_indexer = NotesIndexer(
        pipeline=IndexingPipeline(processors=processors, config={})
    )

    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=_TopicEmbeddingGenerator(),
        shutdown_event_instance=shutdown_event,
        engine=pg_vector_db_engine,
    )
    worker.register_task_handler("index_note", notes_indexer.handle_index_note)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)
    worker_task = asyncio.create_task(worker.run(new_task_event))

    db = Database(engine=pg_vector_db_engine)
    try:
        for title, content in (
            (TEST_NOTE_TITLE, TEST_NOTE_CONTENT),
            (DISTRACTOR_NOTE_TITLE, DISTRACTOR_NOTE_CONTENT),
        ):
            result = await db.notes.add_or_update(
                title=title,
                content=content,
                write_policy=NoteWritePolicy.UNCONSTRAINED,
                provenance=NoteProvenanceStamp.internal(),
            )
            assert result == "Success", f"Failed to create note {title!r}: {result}"

        new_task_event.set()
        await wait_for_tasks_to_complete(pg_vector_db_engine, timeout_seconds=20.0)

        semantic_results = await query_vectors(
            db,
            query_embedding=SEMANTIC_QUERY_VECTOR,
            embedding_model=TEST_EMBEDDING_MODEL,
            limit=5,
            filters={"source_type": "note"},
            embedding_type_filter=["raw_note_text"],
        )
        assert [r["title"] for r in semantic_results] == [
            TEST_NOTE_TITLE,
            DISTRACTOR_NOTE_TITLE,
        ]
        top_semantic = semantic_results[0]
        assert top_semantic["source_type"] == "note"
        assert top_semantic["embedding_type"] == "raw_note_text"
        assert float(top_semantic["distance"]) < 0.01
        assert float(semantic_results[1]["distance"]) > 0.5
        indexed_text = top_semantic["embedding_source_content"]
        assert TEST_NOTE_TITLE in indexed_text
        assert "Superposition: Qubits can exist in multiple states" in indexed_text

        keyword_results = await query_vectors(
            db,
            query_embedding=KEYWORD_QUERY_VECTOR,
            embedding_model=TEST_EMBEDDING_MODEL,
            keywords=TEST_QUERY_KEYWORD,
            limit=5,
            filters={"source_type": "note"},
            embedding_type_filter=["raw_note_text"],
        )
        assert [r["title"] for r in keyword_results] == [
            TEST_NOTE_TITLE,
            DISTRACTOR_NOTE_TITLE,
        ]
        assert keyword_results[0]["fts_score"] > 0
        assert keyword_results[1]["fts_score"] is None

        retrieved_note = await db.notes.get_by_title(
            TEST_NOTE_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
        )
        assert retrieved_note is not None
        assert retrieved_note.content == TEST_NOTE_CONTENT
    finally:
        shutdown_event.set()
        new_task_event.set()
        await asyncio.wait_for(worker_task, timeout=10.0)
