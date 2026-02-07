"""
End-to-end functional test for the notes indexing pipeline.
Tests the complete flow: note creation -> automatic indexing -> vector search.
"""

import asyncio
import logging
import uuid
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import Text, select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.notes_indexer import NotesIndexer
from family_assistant.indexing.pipeline import ContentProcessor, IndexingPipeline
from family_assistant.indexing.processors import EmbeddingDispatchProcessor
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import query_vectors
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete


def _create_mock_processing_service() -> MagicMock:
    """Create a mock ProcessingService with required attributes."""
    mock = MagicMock()
    return mock


logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_EMBEDDING_MODEL = "mock-notes-model"
TEST_EMBEDDING_DIMENSION = 128
TEST_NOTE_TITLE = "Test Quantum Computing Research"
TEST_NOTE_CONTENT = """
Quantum computing leverages quantum mechanical phenomena such as superposition and entanglement to process information.

Key concepts include:
- Qubits: The basic unit of quantum information
- Superposition: Qubits can exist in multiple states simultaneously
- Entanglement: Quantum particles become correlated in ways that classical physics cannot explain

Applications in cryptography and optimization are particularly promising for the next decade.
"""

TEST_QUERY_SEMANTIC = "quantum entanglement applications"
TEST_QUERY_KEYWORD = "superposition qubits"


@pytest_asyncio.fixture(scope="function")
async def mock_embedding_generator_notes() -> MockEmbeddingGenerator:
    """Provides a mock embedding generator configured for notes testing."""
    # Use a fixed seed for reproducible embeddings
    np.random.seed(42)

    # Create deterministic embeddings for the note content
    note_title_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.1
    ).tolist()
    note_content_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.2
    ).tolist()

    # Create query embeddings close to content for semantic matching
    semantic_query_embedding = (
        np.array(note_content_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()
    keyword_query_embedding = (
        np.array(note_content_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.02
    ).tolist()

    # The note indexer combines title and content, so we need embedding for that
    # We'll use a function to generate embeddings for unknown texts
    def generate_embedding_for_text(text: str) -> list[float]:
        if TEST_NOTE_TITLE in text and TEST_NOTE_CONTENT in text:
            # This is the combined note text
            return note_content_embedding
        return np.zeros(TEST_EMBEDDING_DIMENSION).tolist()

    embedding_map = {
        TEST_NOTE_TITLE: note_title_embedding,
        TEST_NOTE_CONTENT: note_content_embedding,
        TEST_QUERY_SEMANTIC: semantic_query_embedding,
        TEST_QUERY_KEYWORD: keyword_query_embedding,
    }

    generator = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="generate",  # Generate embeddings for unknown texts
    )

    # Store query embeddings for later use
    generator.embedding_map["__semantic_query_embedding__"] = semantic_query_embedding
    generator.embedding_map["__keyword_query_embedding__"] = keyword_query_embedding

    return generator


async def _helper_handle_embed_and_store_batch_notes(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    """Helper task handler for embedding and storing batches during notes testing."""
    logger.info(
        f"Notes test task handler 'handle_embed_and_store_batch' received payload: {payload}"
    )
    db_context = exec_context.db_context
    embedding_generator = exec_context.embedding_generator
    assert embedding_generator is not None, (
        "Embedding generator not found in exec_context"
    )

    document_id = payload["document_id"]
    texts_to_embed: list[str] = payload["texts_to_embed"]
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    embedding_metadata_list: list[dict[str, Any]] = payload["embedding_metadata_list"]

    if not texts_to_embed:
        logger.warning(
            f"No texts to embed for document {document_id} in 'embed_and_store_batch'."
        )
        return

    embedding_result = await embedding_generator.generate_embeddings(texts_to_embed)

    for i, vector in enumerate(embedding_result.embeddings):
        meta = embedding_metadata_list[i]
        await db_context.vector.add_embedding(
            document_id=document_id,
            chunk_index=meta.get("chunk_index", 0),
            embedding_type=meta["embedding_type"],
            embedding=vector,
            embedding_model=embedding_result.model_name,
            content=texts_to_embed[i],
            embedding_doc_metadata=meta.get("original_content_metadata"),
        )
    logger.info(
        f"Stored {len(texts_to_embed)} embeddings for note document {document_id}."
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_indexing_e2e(
    pg_vector_db_engine: AsyncEngine,
    mock_embedding_generator_notes: MockEmbeddingGenerator,
) -> None:
    """
    End-to-end test for notes indexing and vector search.

    1. Create a note using add_or_update_note (triggers indexing task)
    2. Setup NotesIndexer and TaskWorker
    3. Wait for indexing task completion
    4. Verify note is indexed and searchable via vector query
    5. Test both semantic and keyword search
    """
    logger.info("\n--- Running Notes Indexing E2E Test ---")

    # Generate unique note title to avoid conflicts
    unique_note_title = f"{TEST_NOTE_TITLE} {uuid.uuid4()}"

    # --- Arrange: Setup Pipeline and Indexer ---
    # Create pipeline with processors

    processors: list[ContentProcessor] = [
        EmbeddingDispatchProcessor(
            embedding_types_to_dispatch=["raw_note_text"],
        )
    ]

    pipeline = IndexingPipeline(
        processors=processors,
        config={},  # Empty config for test
    )
    notes_indexer = NotesIndexer(pipeline=pipeline)
    logger.info("NotesIndexer initialized with test pipeline config.")

    # --- Arrange: Setup TaskWorker ---
    mock_chat_interface = MagicMock()

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator_notes,
        engine=pg_vector_db_engine,  # Pass the database engine
    )

    # Register task handlers
    worker.register_task_handler("index_note", notes_indexer.handle_index_note)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)
    logger.info("TaskWorker created and 'index_note' task handler registered.")

    # --- Act: Start Background Worker ---
    worker_id = f"test-notes-worker-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    logger.info(f"Started background task worker {worker_id}...")
    await asyncio.sleep(0.1)  # Give worker time to start

    note_id = None
    indexing_task_id = None
    document_db_id = None

    try:
        # --- Act: Create Note (triggers indexing task) ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            result = await db_context.notes.add_or_update(
                title=unique_note_title,
                content=TEST_NOTE_CONTENT,
            )
            assert result == "Success", f"Failed to create note: {result}"
            logger.info(f"Created note with title: {unique_note_title}")

            # Get the note ID for tracking

            note_stmt = select(notes_table.c.id).where(
                notes_table.c.title == unique_note_title
            )
            note_row = await db_context.fetch_one(note_stmt)
            assert note_row is not None, "Note not found after creation"
            note_id = note_row["id"]
            logger.info(f"Note created with ID: {note_id}")

        # --- Act: Find the Indexing Task ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)  # Wait for task to be enqueued

            select_task_stmt = (
                select(tasks_table.c.task_id)
                .where(
                    tasks_table.c.task_type == "index_note",
                    tasks_table.c.payload.cast(Text).like(f'%"note_id": {note_id}%'),
                )
                .order_by(tasks_table.c.created_at.desc())
                .limit(1)
            )
            task_info = await db.fetch_one(select_task_stmt)
            assert task_info is not None, (
                f"Could not find enqueued indexing task for note ID {note_id}"
            )
            indexing_task_id = task_info["task_id"]
            logger.info(
                f"Found indexing task ID: {indexing_task_id} for note ID: {note_id}"
            )

        # --- Act: Wait for Task Completion ---
        test_new_task_event.set()  # Signal worker to check for tasks
        logger.info(f"Waiting for indexing task {indexing_task_id} to complete...")
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            task_ids={indexing_task_id},
            timeout_seconds=20.0,
        )
        logger.info(f"Indexing task {indexing_task_id} completed.")

        # --- Assert: Find Document Record ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            # Find the document that was created for our note
            doc_record = await db.vector.get_document_by_source_id(
                unique_note_title
            )  # Notes use title as source_id
            assert doc_record is not None, (
                f"Document record not found for note with title: {unique_note_title}"
            )
            document_db_id = doc_record.id
            assert doc_record.source_type == "note"
            assert doc_record.title == unique_note_title
            logger.info(f"Found document record with ID: {document_db_id}")

        # --- Assert: Semantic Query ---
        semantic_query_embedding = mock_embedding_generator_notes.embedding_map[
            "__semantic_query_embedding__"
        ]

        semantic_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(
                f"Querying vectors using semantic text: '{TEST_QUERY_SEMANTIC}'"
            )
            semantic_query_results = await query_vectors(
                db,
                query_embedding=semantic_query_embedding,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"source_type": "note"},
                embedding_type_filter=["raw_note_text"],
            )

        assert semantic_query_results is not None, "Semantic query returned None"
        assert len(semantic_query_results) > 0, "No results from semantic query"
        logger.info(f"Semantic query returned {len(semantic_query_results)} result(s).")

        # Find our note in the results
        found_semantic_result = None
        for result in semantic_query_results:
            if result.get("source_id") == unique_note_title:
                found_semantic_result = result
                break

        assert found_semantic_result is not None, (
            f"Note with title '{unique_note_title}' not found in semantic query results"
        )
        logger.info(f"Found note in semantic results: {found_semantic_result}")

        # Verify result structure
        assert found_semantic_result.get("source_type") == "note"
        assert found_semantic_result.get("title") == unique_note_title
        assert found_semantic_result.get("embedding_type") == "raw_note_text"
        assert "distance" in found_semantic_result
        # Skip distance check if using SQLite (returns nan)
        if pg_vector_db_engine.dialect.name == "postgresql":
            assert float(found_semantic_result["distance"]) < 0.5, (
                f"Semantic distance should be small, got {found_semantic_result['distance']}"
            )

        # Verify the content contains both title and note content
        embedding_content = found_semantic_result.get("embedding_source_content", "")
        assert unique_note_title in embedding_content
        assert "quantum" in embedding_content.lower()
        assert "superposition" in embedding_content.lower()

        # --- Assert: Keyword Query ---
        keyword_query_embedding = mock_embedding_generator_notes.embedding_map[
            "__keyword_query_embedding__"
        ]

        keyword_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(f"Querying vectors using keyword text: '{TEST_QUERY_KEYWORD}'")
            keyword_query_results = await query_vectors(
                db,
                query_embedding=keyword_query_embedding,
                embedding_model=TEST_EMBEDDING_MODEL,
                keywords=TEST_QUERY_KEYWORD,  # Add FTS keywords
                limit=5,
                filters={"source_type": "note"},
                embedding_type_filter=["raw_note_text"],
            )

        assert keyword_query_results is not None, "Keyword query returned None"
        assert len(keyword_query_results) > 0, "No results from keyword query"
        logger.info(f"Keyword query returned {len(keyword_query_results)} result(s).")

        # Find our note in keyword results
        found_keyword_result = None
        for result in keyword_query_results:
            if result.get("source_id") == unique_note_title:
                found_keyword_result = result
                break

        assert found_keyword_result is not None, (
            f"Note with title '{unique_note_title}' not found in keyword query results"
        )
        logger.info(f"Found note in keyword results: {found_keyword_result}")

        # Verify keyword-specific fields
        assert "rrf_score" in found_keyword_result, (
            "Missing RRF score in keyword results"
        )
        assert "fts_score" in found_keyword_result, (
            "Missing FTS score in keyword results"
        )
        assert found_keyword_result["fts_score"] > 0, "FTS score should be positive"

        # --- Assert: Verify Note Content Accessibility ---
        # Verify that the original note content is still accessible via storage
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            retrieved_note = await db.notes.get_by_title(
                unique_note_title, visibility_grants=None
            )
            assert retrieved_note is not None, "Could not retrieve original note"
            assert retrieved_note.content == TEST_NOTE_CONTENT
            logger.info("Verified original note content is still accessible")

        logger.info("--- Notes Indexing E2E Test Passed ---")

    finally:
        # --- Cleanup ---
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} stopped.")
        except TimeoutError:
            logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                logger.info(f"Worker task {worker_id} cancellation confirmed.")

        # Clean up test data
        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.vector.delete_document(document_db_id)
                    logger.info(f"Cleaned up test document DB ID {document_db_id}")
            except Exception as cleanup_err:
                logger.warning(f"Error during document cleanup: {cleanup_err}")

        if note_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.notes.delete(unique_note_title)
                    logger.info(f"Cleaned up test note: {unique_note_title}")
            except Exception as cleanup_err:
                logger.warning(f"Error during note cleanup: {cleanup_err}")

        if indexing_task_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    delete_stmt = tasks_table.delete().where(
                        tasks_table.c.task_id == indexing_task_id
                    )
                    await db_cleanup.execute_with_retry(delete_stmt)
                    logger.info(f"Cleaned up test task ID {indexing_task_id}")
            except Exception as cleanup_err:
                logger.warning(f"Error during task cleanup: {cleanup_err}")
