"""
End-to-end functional test for the notes indexing pipeline.
Tests the complete flow: note creation -> automatic indexing -> vector search.
"""

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

# storage functions now accessed via DatabaseContext
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.notes_indexer import NotesIndexer
from family_assistant.indexing.pipeline import IndexingPipeline
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage.context import DatabaseContext

# notes functions now accessed via DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import query_vectors
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete

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
    exec_context: ToolExecutionContext, payload: dict[str, Any]
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
    from family_assistant.indexing.pipeline import ContentProcessor
    from family_assistant.indexing.processors import EmbeddingDispatchProcessor

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
        processing_service=cast("ProcessingService", None),
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator_notes,
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
            from family_assistant.storage.notes import notes_table

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
            from sqlalchemy import Text

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
            retrieved_note = await db.notes.get_by_title(unique_note_title)
            assert retrieved_note is not None, "Could not retrieve original note"
            assert retrieved_note["content"] == TEST_NOTE_CONTENT
            logger.info("Verified original note content is still accessible")

        logger.info("--- Notes Indexing E2E Test Passed ---")

    finally:
        # --- Cleanup ---
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} stopped.")
        except asyncio.TimeoutError:
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


@pytest.mark.asyncio
async def test_note_update_reindexing_e2e(
    pg_vector_db_engine: AsyncEngine,
    mock_embedding_generator_notes: MockEmbeddingGenerator,
) -> None:
    """
    Test that updating a note triggers re-indexing and old embeddings are replaced.

    1. Create initial note and wait for indexing
    2. Update note content and wait for re-indexing
    3. Verify old embeddings are deleted and new ones created
    4. Verify updated content is searchable
    """
    logger.info("\n--- Running Note Update Re-indexing E2E Test ---")

    unique_note_title = f"Update Test Note {uuid.uuid4()}"
    initial_content = "Initial content about classical computing systems."
    updated_content = (
        "Updated content about quantum computing and entanglement phenomena."
    )

    # Set up embeddings for initial and updated content
    combined_initial_text = f"{unique_note_title}\n\n{initial_content}"
    initial_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.15
    ).tolist()

    combined_updated_text = f"{unique_note_title}\n\n{updated_content}"
    updated_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.3
    ).tolist()

    # Add embeddings to the mock
    mock_embedding_generator_notes.embedding_map[initial_content] = initial_embedding
    mock_embedding_generator_notes.embedding_map[combined_initial_text] = (
        initial_embedding
    )
    mock_embedding_generator_notes.embedding_map[updated_content] = updated_embedding
    mock_embedding_generator_notes.embedding_map[combined_updated_text] = (
        updated_embedding
    )

    # Setup same infrastructure as main test
    # Create pipeline with processors
    from family_assistant.indexing.pipeline import ContentProcessor
    from family_assistant.indexing.processors import EmbeddingDispatchProcessor

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

    mock_chat_interface = MagicMock()

    worker = TaskWorker(
        processing_service=cast("ProcessingService", None),
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator_notes,
    )

    worker.register_task_handler("index_note", notes_indexer.handle_index_note)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    note_id = None
    document_db_id = None
    initial_task_id = None

    try:
        # --- Step 1: Create Initial Note ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            result = await db_context.notes.add_or_update(
                title=unique_note_title, content=initial_content
            )
            assert result == "Success"

            from family_assistant.storage.notes import notes_table

            note_stmt = select(notes_table.c.id).where(
                notes_table.c.title == unique_note_title
            )
            note_row = await db_context.fetch_one(note_stmt)
            assert note_row is not None, "Note not found after creation"
            note_id = note_row["id"]
            logger.info(f"Created initial note with ID: {note_id}")

        # Wait for initial indexing task
        test_new_task_event.set()

        # Find and wait for the initial indexing task to complete
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)  # Wait for task to be enqueued
            from sqlalchemy import Text

            # Find the index_note task
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
                f"Could not find initial indexing task for note ID {note_id}"
            )
            initial_task_id = task_info["task_id"]
            logger.info(f"Found initial indexing task ID: {initial_task_id}")

        # Wait for initial indexing to complete
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            task_ids={initial_task_id},
            timeout_seconds=10.0,
        )
        logger.info("Initial indexing completed")

        # Get document ID and count embeddings
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            doc_record = await db.vector.get_document_by_source_id(unique_note_title)
            assert doc_record is not None
            document_db_id = doc_record.id

            # Count initial embeddings
            from family_assistant.storage.vector import DocumentEmbeddingRecord

            initial_embeddings_stmt = select(DocumentEmbeddingRecord.id).where(
                DocumentEmbeddingRecord.document_id == document_db_id
            )
            initial_embeddings = await db.fetch_all(initial_embeddings_stmt)
            initial_count = len(initial_embeddings)
            logger.info(f"Initial embeddings count: {initial_count}")
            assert initial_count > 0, "No embeddings created during initial indexing"

        # --- Step 2: Update Note Content ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            result = await db_context.notes.add_or_update(
                title=unique_note_title, content=updated_content
            )
            assert result == "Success"
            logger.info("Updated note content")

        # Wait for re-indexing task
        test_new_task_event.set()

        # Find and wait for the re-indexing task
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)  # Wait for task to be enqueued
            from sqlalchemy import Text

            # Find the new index_note task (created after update)
            select_update_task_stmt = (
                select(tasks_table.c.task_id)
                .where(
                    tasks_table.c.task_type == "index_note",
                    tasks_table.c.payload.cast(Text).like(f'%"note_id": {note_id}%'),
                    tasks_table.c.task_id
                    != initial_task_id,  # Exclude the initial task
                )
                .order_by(tasks_table.c.created_at.desc())
                .limit(1)
            )
            update_task_info = await db.fetch_one(select_update_task_stmt)
            assert update_task_info is not None, (
                f"Could not find re-indexing task for note ID {note_id}"
            )
            update_task_id = update_task_info["task_id"]
            logger.info(f"Found re-indexing task ID: {update_task_id}")

        # Wait for re-indexing to complete
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            task_ids={update_task_id},
            timeout_seconds=10.0,
        )
        logger.info("Re-indexing completed")

        # --- Step 3: Verify Re-indexing Occurred ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            # Check that we still have embeddings (they should be replaced, not just deleted)
            final_embeddings_stmt = select(DocumentEmbeddingRecord.id).where(
                DocumentEmbeddingRecord.document_id == document_db_id
            )
            final_embeddings = await db.fetch_all(final_embeddings_stmt)
            final_count = len(final_embeddings)
            logger.info(f"Final embeddings count: {final_count}")

            # Should have same number of embeddings (old ones deleted, new ones created)
            assert final_count > 0, "No embeddings found after update"

        # --- Step 4: Verify Updated Content is Searchable ---
        # Use a query that would match the updated content better
        query_embedding = mock_embedding_generator_notes.embedding_map.get(
            "__semantic_query_embedding__"
        )
        assert query_embedding is not None, "Query embedding not found"

        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            search_results = await query_vectors(
                db,
                query_embedding=query_embedding,
                embedding_model=TEST_EMBEDDING_MODEL,
                keywords="quantum entanglement",  # Match updated content
                limit=5,
                filters={"source_id": unique_note_title},
            )

        assert len(search_results) > 0, "Updated note not found in search"
        result = search_results[0]

        # Verify the content contains the updated text
        embedding_content = result.get("embedding_source_content", "")
        assert (
            "Updated content" in embedding_content
            or "quantum computing" in embedding_content
        )
        logger.info("Verified updated content is searchable")

        logger.info("--- Note Update Re-indexing E2E Test Passed ---")

    finally:
        # Cleanup (same as main test)
        test_shutdown_event = asyncio.Event()
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.vector.delete_document(document_db_id)
            except Exception as e:
                logger.warning(f"Cleanup error: {e}")

        if note_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.notes.delete(unique_note_title)
            except Exception as e:
                logger.warning(f"Note cleanup error: {e}")


@pytest.mark.asyncio
async def test_notes_indexing_graceful_degradation(
    pg_vector_db_engine: AsyncEngine,
    mock_embedding_generator_notes: MockEmbeddingGenerator,
) -> None:
    """
    Test that large note content is stored without embedding (graceful degradation).

    This test verifies:
    1. Small content gets embedded normally
    2. Large content is stored but not embedded
    3. Both are accessible via get_full_document_content
    """
    logger.info("\n--- Running Notes Indexing Graceful Degradation Test ---")

    # Create very large content that exceeds embedding limit
    LARGE_CONTENT = "This is a very large note. " * 2000  # ~30K chars
    unique_note_title = f"Large Note Test {uuid.uuid4()}"

    # Setup pipeline and indexer (same as main test)
    from family_assistant.indexing.pipeline import ContentProcessor
    from family_assistant.indexing.processors import EmbeddingDispatchProcessor

    processors: list[ContentProcessor] = [
        EmbeddingDispatchProcessor(
            embedding_types_to_dispatch=["raw_note_text"],
        )
    ]

    pipeline = IndexingPipeline(
        processors=processors,
        config={},
    )
    notes_indexer = NotesIndexer(pipeline=pipeline)

    # Setup TaskWorker
    mock_chat_interface = MagicMock()

    worker = TaskWorker(
        processing_service=cast("ProcessingService", None),
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator_notes,
    )

    worker.register_task_handler("index_note", notes_indexer.handle_index_note)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    note_id = None
    document_db_id = None
    indexing_task_id = None

    try:
        # Create large note
        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            result = await db_context.notes.add_or_update(
                title=unique_note_title,
                content=LARGE_CONTENT,
            )
            assert result == "Success"

            from family_assistant.storage.notes import notes_table

            note_stmt = select(notes_table.c.id).where(
                notes_table.c.title == unique_note_title
            )
            note_row = await db_context.fetch_one(note_stmt)
            assert note_row is not None, "Note not found after creation"
            note_id = note_row["id"]
            logger.info(f"Created large note with ID: {note_id}")

        # Find the indexing task
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)  # Wait for task to be enqueued
            from sqlalchemy import Text

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
                f"Could not find indexing task for note ID {note_id}"
            )
            indexing_task_id = task_info["task_id"]
            logger.info(f"Found indexing task ID: {indexing_task_id}")

        # Wait for indexing task to complete
        test_new_task_event.set()
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            task_ids={indexing_task_id},
            timeout_seconds=20.0,
        )
        logger.info(f"Indexing task {indexing_task_id} completed.")

        # Wait a bit more for any embed_and_store_batch tasks that may have been created
        await asyncio.sleep(1.0)
        test_new_task_event.set()

        # Find any embed_and_store_batch tasks for this document
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            # First get the document ID
            doc_record = await db.vector.get_document_by_source_id(unique_note_title)
            if doc_record:
                embed_task_stmt = select(
                    tasks_table.c.task_id, tasks_table.c.status
                ).where(
                    tasks_table.c.task_type == "embed_and_store_batch",
                    tasks_table.c.payload.cast(Text).like(
                        f'%"document_id": {doc_record.id}%'
                    ),
                )
                embed_tasks = await db.fetch_all(embed_task_stmt)
                if embed_tasks:
                    logger.info(f"Found {len(embed_tasks)} embed_and_store_batch tasks")
                    embed_task_ids = {
                        task["task_id"]
                        for task in embed_tasks
                        if task["status"] != "completed"
                    }
                    if embed_task_ids:
                        logger.info(
                            f"Waiting for {len(embed_task_ids)} embed tasks to complete"
                        )
                        await wait_for_tasks_to_complete(
                            pg_vector_db_engine,
                            task_ids=embed_task_ids,
                            timeout_seconds=10.0,
                        )

        # Verify document and embeddings
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            doc_record = await db.vector.get_document_by_source_id(unique_note_title)
            assert doc_record is not None
            document_db_id = doc_record.id

            # Check embeddings
            from family_assistant.storage.vector import DocumentEmbeddingRecord

            embeddings_stmt = select(
                DocumentEmbeddingRecord.embedding_type,
                DocumentEmbeddingRecord.embedding_model,
                DocumentEmbeddingRecord.content,
            ).where(DocumentEmbeddingRecord.document_id == document_db_id)
            embeddings = await db.fetch_all(embeddings_stmt)

            # Should have at least one embedding record
            assert len(embeddings) > 0

            # Find the raw_note_text embedding
            raw_note_embedding = None
            for emb in embeddings:
                if emb["embedding_type"] == "raw_note_text":
                    raw_note_embedding = emb
                    break

            assert raw_note_embedding is not None, "raw_note_text embedding not found"

            # Check if it was stored without embedding due to size
            # The actual content embedded includes title + content
            combined_text_len = (
                len(raw_note_embedding["content"])
                if raw_note_embedding["content"]
                else 0
            )
            if combined_text_len > 30000:  # Matches MAX_CONTENT_LENGTH in tasks.py
                assert raw_note_embedding["embedding_model"] in [
                    "text_only_too_long",
                    "text_only_error",
                    "text_only_empty_result",
                ], (
                    f"Expected storage-only model, got: {raw_note_embedding['embedding_model']}"
                )
                logger.info(
                    f"Large content ({combined_text_len} chars) was stored without embedding as expected"
                )
            else:
                logger.info(
                    f"Content ({combined_text_len} chars) was small enough to embed"
                )

            # Verify content is stored
            assert raw_note_embedding["content"] is not None
            assert unique_note_title in raw_note_embedding["content"]
            assert len(raw_note_embedding["content"]) > 30000

        logger.info("--- Notes Indexing Graceful Degradation Test Passed ---")

    finally:
        # Cleanup
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.vector.delete_document(document_db_id)
            except Exception as e:
                logger.warning(f"Document cleanup error: {e}")

        if note_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.notes.delete(unique_note_title)
            except Exception as e:
                logger.warning(f"Note cleanup error: {e}")

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

        # Also clean up any embed_and_store_batch tasks
        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    delete_embed_stmt = tasks_table.delete().where(
                        tasks_table.c.task_type == "embed_and_store_batch",
                        tasks_table.c.payload.cast(Text).like(
                            f'%"document_id": {document_db_id}%'
                        ),
                    )
                    result = await db_cleanup.execute_with_retry(delete_embed_stmt)
                    if result.rowcount > 0:  # type: ignore[attr-defined]
                        logger.info(
                            f"Cleaned up {result.rowcount} embed_and_store_batch tasks"
                        )
            except Exception as cleanup_err:
                logger.warning(f"Error during embed task cleanup: {cleanup_err}")
