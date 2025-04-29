"""
End-to-end functional tests for the document indexing and vector search pipeline,
simulating the flow initiated by the /api/documents/upload endpoint.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional, List # Add missing typing imports

import numpy as np
import pytest
from sqlalchemy import select

# Import components needed for the E2E test
from family_assistant import storage
from family_assistant.task_worker import TaskWorker, shutdown_event, new_task_event
from family_assistant.embeddings import MockEmbeddingGenerator, EmbeddingGenerator # Import protocol
from family_assistant.indexing.document_indexer import DocumentIndexer # Import the class
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import query_vectors, DocumentRecord, Document # Import Document protocol
from family_assistant.storage.tasks import tasks_table, enqueue_task

# Import test helpers
from tests.helpers import wait_for_tasks_to_complete

logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_EMBEDDING_MODEL = "mock-e2e-doc-model"
TEST_EMBEDDING_DIMENSION = 128  # Smaller dimension for mock testing


# --- Test Data for E2E ---
TEST_DOC_SOURCE_TYPE = "manual_test_upload"
TEST_DOC_SOURCE_ID = f"test-doc-{uuid.uuid4()}"
TEST_DOC_TITLE = "E2E Test: Project Phoenix Proposal"
TEST_DOC_CHUNK_0 = "This document outlines the proposal for Project Phoenix, focusing on renewable energy sources."
TEST_DOC_CHUNK_1 = "Key areas include solar panel efficiency and battery storage improvements."
TEST_DOC_METADATA = {"author": "test_user", "version": 1.1}
TEST_DOC_CREATED_AT = datetime(2024, 5, 15, 10, 0, 0, tzinfo=timezone.utc)

# Content parts dictionary mimicking the structure expected by the indexer task
TEST_DOC_CONTENT_PARTS = {
    "title": TEST_DOC_TITLE,
    "content_chunk_0": TEST_DOC_CHUNK_0,
    "content_chunk_1": TEST_DOC_CHUNK_1,
}

TEST_QUERY_TEXT_SEMANTIC = "information about solar panels" # Relevant to chunk 1
TEST_QUERY_TEXT_KEYWORD = "Phoenix proposal" # Relevant to title and chunk 0


# --- Helper Function for Test Setup ---

# Define a simple class conforming to the Document protocol for the helper
class SimpleTestDocument(Document):
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    @property
    def source_type(self) -> str: return self._data["source_type"]
    @property
    def source_id(self) -> str: return self._data["source_id"]
    @property
    def source_uri(self) -> Optional[str]: return self._data.get("source_uri")
    @property
    def title(self) -> Optional[str]: return self._data.get("title")
    @property
    def created_at(self) -> Optional[datetime]: return self._data.get("created_at")
    @property
    def metadata(self) -> Optional[Dict[str, Any]]: return self._data.get("metadata")


async def _ingest_and_index_document(
    engine,
    doc_data: Dict[str, Any], # Contains source_id, title, etc.
    content_parts: Dict[str, str],
    task_timeout: float = 15.0,
    notify_event: Optional[asyncio.Event] = None
) -> Tuple[int, str]:
    """
    Helper to simulate document ingestion: adds document record, enqueues task,
    notifies worker, waits for completion, and returns IDs.

    Args:
        engine: The database engine fixture.
        doc_data: Dictionary with metadata for the 'documents' table.
        content_parts: Dictionary with content parts for the indexing task payload.
        task_timeout: Timeout for waiting for the task.
        notify_event: Event to signal the worker.

    Returns:
        A tuple containing (document_db_id, indexing_task_id).
    """
    document_db_id = None
    indexing_task_id = f"test-index-doc-{doc_data['source_id']}-{uuid.uuid4()}" # Generate unique task ID
    source_id = doc_data.get("source_id", "UNKNOWN_SOURCE_ID")

    async with DatabaseContext(engine=engine) as db:
        logger.info(f"Helper: Adding document record for source_id: {source_id}")
        # Create a Document object conforming to the protocol
        doc_for_storage = SimpleTestDocument(doc_data)
        document_db_id = await storage.add_document(
            db,
            doc=doc_for_storage,
            enriched_doc_metadata=None # Assuming metadata is already in doc_data['metadata']
        )
        assert document_db_id is not None, f"Failed to get DB ID for document {source_id}"
        logger.info(f"Helper: Document record added (DB ID: {document_db_id})")

        # Enqueue the processing task
        task_payload = {
            "document_id": document_db_id,
            "content_parts": content_parts,
        }
        logger.info(f"Helper: Enqueuing 'process_uploaded_document' task (ID: {indexing_task_id}) for DB ID {document_db_id}")
        await enqueue_task(
            db_context=db,
            task_id=indexing_task_id,
            task_type="process_uploaded_document",
            payload=task_payload,
            notify_event=notify_event # Pass the event
        )
        logger.info(f"Helper: Task {indexing_task_id} enqueued.")

    # Wait for the specific task to complete
    logger.info(f"Helper: Waiting for task {indexing_task_id} to complete...")
    await wait_for_tasks_to_complete(
        engine, task_ids={indexing_task_id}, timeout_seconds=task_timeout
    )
    logger.info(f"Helper: Task {indexing_task_id} reported as complete.")

    return document_db_id, indexing_task_id


# --- Test Functions ---

@pytest.mark.asyncio
async def test_document_indexing_and_query_e2e(pg_vector_db_engine):
    """
    End-to-end test for document ingestion via simulated API call, indexing
    via task worker, and vector/keyword query retrieval.
    1. Setup Mock Embedder.
    2. Instantiate DocumentIndexer with mock embedder.
    3. Instantiate TaskWorker and register the 'process_uploaded_document' handler.
    4. Simulate document ingestion using the helper function (adds doc, enqueues task).
    5. Start the task worker loop in the background.
    6. Wait for the specific indexing task to complete using the helper.
    7. Stop the task worker loop.
    8. Generate query embeddings for relevant text/keywords.
    9. Execute query_vectors for semantic and keyword searches.
    10. Verify the ingested document is found in the results.
    """
    logger.info("\n--- Running Document Indexing E2E Test ---")

    # --- Arrange: Mock Embeddings ---
    # Create deterministic embeddings for known text parts
    title_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.1
    ).tolist()
    chunk0_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.2
    ).tolist()
    chunk1_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.3
    ).tolist()
    # Make semantic query embedding closer to chunk1 embedding
    query_semantic_embedding = (np.array(chunk1_embedding) + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01).tolist()
    # Make keyword query embedding closer to title embedding
    query_keyword_embedding = (np.array(title_embedding) + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.02).tolist()


    embedding_map = {
        TEST_DOC_TITLE: title_embedding,
        TEST_DOC_CHUNK_0: chunk0_embedding,
        TEST_DOC_CHUNK_1: chunk1_embedding,
        TEST_QUERY_TEXT_SEMANTIC: query_semantic_embedding,
        TEST_QUERY_TEXT_KEYWORD: query_keyword_embedding,
    }
    mock_embedder = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(), # Default if needed
    )

    # --- Arrange: Instantiate Indexer ---
    document_indexer = DocumentIndexer(embedding_generator=mock_embedder)
    logger.info(f"DocumentIndexer initialized with mock embedding generator ({TEST_EMBEDDING_MODEL}).")

    # --- Arrange: Register Task Handler ---
    # Create a TaskWorker instance for this test and register the handler
    worker = TaskWorker(processing_service=None) # No processing service needed for this handler
    worker.register_task_handler(
        "process_uploaded_document", document_indexer.process_document # Register the method
    )
    logger.info("TaskWorker created and 'process_uploaded_document' task handler registered.")

    # --- Act: Start Background Worker ---
    worker_id = f"test-doc-worker-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event() # Use local event for worker control
    test_new_task_event = asyncio.Event() # Worker will wait on this

    worker_task = asyncio.create_task(
        worker.run(test_new_task_event) # Pass the event to the worker's run method
    )
    logger.info(f"Started background task worker {worker_id}...")
    await asyncio.sleep(0.1) # Give worker time to start

    document_db_id = None
    indexing_task_id = None
    try:
        # --- Act: Ingest Document and Wait for Indexing ---
        doc_data_to_store = {
            "source_type": TEST_DOC_SOURCE_TYPE,
            "source_id": TEST_DOC_SOURCE_ID,
            "title": TEST_DOC_TITLE,
            "created_at": TEST_DOC_CREATED_AT,
            "metadata": TEST_DOC_METADATA,
            # source_uri could be added if needed
        }
        document_db_id, indexing_task_id = await _ingest_and_index_document(
            pg_vector_db_engine,
            doc_data_to_store,
            TEST_DOC_CONTENT_PARTS,
            notify_event=test_new_task_event # Pass the worker's event
        )

        # Wait again to be sure (wait_for_tasks_to_complete handles completion check)
        await wait_for_tasks_to_complete(
             pg_vector_db_engine, task_ids={indexing_task_id}, timeout_seconds=10.0
        )

        # --- Act & Assert: Semantic Query ---
        semantic_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(f"Querying vectors using semantic text: '{TEST_QUERY_TEXT_SEMANTIC}'")
            semantic_query_results = await query_vectors(
                db,
                query_embedding=query_semantic_embedding,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"source_type": TEST_DOC_SOURCE_TYPE} # Filter by source type
            )

        assert semantic_query_results is not None, "Semantic query_vectors returned None"
        assert len(semantic_query_results) > 0, "No results returned from semantic vector query"
        logger.info(f"Semantic query returned {len(semantic_query_results)} result(s).")

        # Find the result corresponding to our document
        found_semantic_result = None
        for result in semantic_query_results:
            if result.get("source_id") == TEST_DOC_SOURCE_ID:
                found_semantic_result = result
                break

        assert (
            found_semantic_result is not None
        ), f"Ingested document (Source ID: {TEST_DOC_SOURCE_ID}) not found in semantic query results: {semantic_query_results}"
        logger.info(f"Found matching semantic result: {found_semantic_result}")

        # Check distance (should be small since query embedding was close to chunk1)
        assert "distance" in found_semantic_result, "Semantic result missing 'distance' field"
        assert found_semantic_result["distance"] < 0.1, f"Semantic distance should be small, but was {found_semantic_result['distance']}"
        # Check the content matched (should be chunk 1)
        assert found_semantic_result.get("embedding_type") == "content_chunk"
        assert found_semantic_result.get("embedding_source_content") == TEST_DOC_CHUNK_1
        assert found_semantic_result.get("title") == TEST_DOC_TITLE
        assert found_semantic_result.get("source_type") == TEST_DOC_SOURCE_TYPE
        assert found_semantic_result.get("doc_metadata") == TEST_DOC_METADATA # Check metadata persistence

        # --- Act & Assert: Keyword Query ---
        keyword_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(f"Querying vectors using keyword text: '{TEST_QUERY_TEXT_KEYWORD}'")
            keyword_query_results = await query_vectors(
                db,
                query_embedding=query_keyword_embedding, # Use keyword-related embedding
                embedding_model=TEST_EMBEDDING_MODEL,
                keywords=TEST_QUERY_TEXT_KEYWORD, # Add keywords for FTS
                limit=5,
                filters={"source_type": TEST_DOC_SOURCE_TYPE}
            )

        assert keyword_query_results is not None, "Keyword query_vectors returned None"
        assert len(keyword_query_results) > 0, "No results returned from keyword vector query"
        logger.info(f"Keyword query returned {len(keyword_query_results)} result(s).")

        # Find the result corresponding to our document (should be ranked high due to keywords)
        found_keyword_result = None
        for result in keyword_query_results:
            if result.get("source_id") == TEST_DOC_SOURCE_ID:
                found_keyword_result = result
                # Check if it's the top result (RRF should prioritize keyword match)
                if keyword_query_results[0].get("source_id") == TEST_DOC_SOURCE_ID:
                    break # Found as top result

        assert (
            found_keyword_result is not None
        ), f"Ingested document (Source ID: {TEST_DOC_SOURCE_ID}) not found in keyword query results: {keyword_query_results}"
        logger.info(f"Found matching keyword result: {found_keyword_result}")

        # Check scores (RRF and FTS should be present)
        assert "rrf_score" in found_keyword_result, "Keyword result missing 'rrf_score' field"
        assert "fts_score" in found_keyword_result, "Keyword result missing 'fts_score' field"
        assert found_keyword_result["fts_score"] > 0, f"Keyword FTS score should be positive, but was {found_keyword_result['fts_score']}"

        # Check the content matched (could be title or chunk 0 due to keywords)
        assert found_keyword_result.get("embedding_type") in ["title", "content_chunk"]
        if found_keyword_result.get("embedding_type") == "title":
            assert found_keyword_result.get("embedding_source_content") == TEST_DOC_TITLE
        else: # content_chunk
            assert found_keyword_result.get("embedding_source_content") == TEST_DOC_CHUNK_0

        assert found_keyword_result.get("title") == TEST_DOC_TITLE
        assert found_keyword_result.get("source_type") == TEST_DOC_SOURCE_TYPE

        logger.info("--- Document Indexing E2E Test Passed ---")

    finally:
        # Stop the worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set() # Signal the worker loop to exit
        # Give the worker time to process the shutdown signal
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} stopped.")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
            worker_task.cancel()
            # Await cancellation if needed
            try:
                await worker_task
            except asyncio.CancelledError:
                logger.info(f"Worker task {worker_id} cancellation confirmed.")
        except Exception as e:
             logger.error(f"Error stopping worker task {worker_id}: {e}", exc_info=True)

        # Optional: Clean up the specific document and task if needed for isolation,
        # though function-scoped fixtures usually handle this.
        if document_db_id:
             try:
                 async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                     await storage.delete_document(db_cleanup, document_db_id)
                     logger.info(f"Cleaned up test document DB ID {document_db_id}")
             except Exception as cleanup_err:
                 logger.warning(f"Error during test document cleanup: {cleanup_err}")
        if indexing_task_id:
             try:
                 async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                     # Manually delete task if necessary (wait_for_tasks should ensure it's done/failed)
                     delete_stmt = tasks_table.delete().where(tasks_table.c.task_id == indexing_task_id)
                     await db_cleanup.execute_with_retry(delete_stmt)
                     logger.info(f"Cleaned up test task ID {indexing_task_id}")
             except Exception as cleanup_err:
                 logger.warning(f"Error during test task cleanup: {cleanup_err}")
