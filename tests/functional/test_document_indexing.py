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

import httpx # Import httpx
import numpy as np
import pytest
import pytest_asyncio # Import pytest_asyncio for async fixtures
import sqlalchemy # Import sqlalchemy for cast
from sqlalchemy import select

# Import components needed for the E2E test
from family_assistant import storage
from family_assistant.task_worker import TaskWorker, shutdown_event, new_task_event
from family_assistant.embeddings import MockEmbeddingGenerator, EmbeddingGenerator # Import protocol
from family_assistant.indexing.document_indexer import DocumentIndexer # Import the class
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import query_vectors, DocumentRecord, Document # Import Document protocol
from family_assistant.storage.tasks import tasks_table, enqueue_task
from family_assistant.web_server import app as fastapi_app # Import the FastAPI app

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
TEST_DOC_CREATED_AT_STR = TEST_DOC_CREATED_AT.isoformat() # String format for API

# Content parts dictionary mimicking the structure expected by the indexer task
TEST_DOC_CONTENT_PARTS = {
    "title": TEST_DOC_TITLE,
    "content_chunk_0": TEST_DOC_CHUNK_0,
    "content_chunk_1": TEST_DOC_CHUNK_1,
}
# Convert dicts to JSON strings for the API call
TEST_DOC_CONTENT_PARTS_JSON = json.dumps(TEST_DOC_CONTENT_PARTS)
TEST_DOC_METADATA_JSON = json.dumps(TEST_DOC_METADATA)


TEST_QUERY_TEXT_SEMANTIC = "information about solar panels" # Relevant to chunk 1
TEST_QUERY_TEXT_KEYWORD = "Phoenix proposal" # Relevant to title and chunk 0


# --- Fixtures ---

@pytest_asyncio.fixture(scope="function")
async def mock_embedding_generator() -> MockEmbeddingGenerator:
    """Provides a function-scoped mock embedding generator instance."""
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
    generator = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(), # Default if needed
    )
    # Store embeddings needed for queries later in the test
    generator._test_query_semantic_embedding = query_semantic_embedding
    generator._test_query_keyword_embedding = query_keyword_embedding
    return generator


@pytest_asyncio.fixture(scope="function")
async def http_client(
    pg_vector_db_engine, # Ensure DB is setup before app starts
    mock_embedding_generator: MockEmbeddingGenerator # Inject the mock generator
) -> httpx.AsyncClient:
    """
    Provides a test client for the FastAPI application, configured with
    the test database and mock embedding generator.
    """
    # Inject the mock embedding generator into the app's state
    # This is crucial so the API endpoint dependency uses the mock
    fastapi_app.state.embedding_generator = mock_embedding_generator
    logger.info("Injected mock embedding generator into FastAPI app state for test client.")

    # The pg_vector_db_engine fixture already patches storage.base.engine
    # so the app will use the correct test database.

    async with httpx.AsyncClient(app=fastapi_app, base_url="http://test") as client:
        yield client
    logger.info("Test HTTP client closed.")
    # Clean up app state if necessary, though function scope might handle it
    if hasattr(fastapi_app.state, "embedding_generator"):
         del fastapi_app.state.embedding_generator


# --- Helper Function for Test Setup (REMOVED) ---
# The API call replaces the need for _ingest_and_index_document


# --- Test Functions ---

@pytest.mark.asyncio
async def test_document_indexing_and_query_e2e(
    pg_vector_db_engine, # Still needed for direct DB checks/queries
    http_client: httpx.AsyncClient, # Use the test client
    mock_embedding_generator: MockEmbeddingGenerator # Get the generator instance
):
    """
    End-to-end test for document ingestion via simulated API call, indexing
    via task worker, and vector/keyword query retrieval.
    1. Setup Mock Embedder (via fixture).
    2. Setup Test HTTP Client (via fixture, configured with mock embedder).
    3. Instantiate DocumentIndexer with the *same* mock embedder.
    4. Instantiate TaskWorker and register the 'process_uploaded_document' handler.
    5. Start the task worker loop in the background.
    6. Call the `/api/documents/upload` endpoint using the HTTP client.
    7. Fetch the enqueued task ID from the database.
    8. Wait for the specific indexing task to complete.
    9. Stop the task worker loop.
    10. Execute query_vectors for semantic and keyword searches.
    11. Verify the ingested document is found in the results.
    """
    logger.info("\n--- Running Document Indexing E2E Test via API ---")

    # --- Arrange: Mock Embeddings (Done via fixture) ---
    # Retrieve query embeddings stored in the fixture instance
    query_semantic_embedding = mock_embedding_generator._test_query_semantic_embedding
    query_keyword_embedding = mock_embedding_generator._test_query_keyword_embedding

    # --- Arrange: Instantiate Indexer ---
    # IMPORTANT: Use the *same* mock generator instance provided by the fixture
    document_indexer = DocumentIndexer(embedding_generator=mock_embedding_generator)
    logger.info(f"DocumentIndexer initialized with mock embedding generator ({mock_embedding_generator.model_name}).")

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
        # --- Act: Call API to Ingest Document ---
        api_form_data = {
            "source_type": TEST_DOC_SOURCE_TYPE,
            "source_id": TEST_DOC_SOURCE_ID,
            "title": TEST_DOC_TITLE,
            "created_at": TEST_DOC_CREATED_AT_STR,
            "metadata": TEST_DOC_METADATA_JSON, # Send as JSON string
            "content_parts": TEST_DOC_CONTENT_PARTS_JSON, # Send as JSON string
            # source_uri could be added if needed
        }
        logger.info(f"Calling POST /api/documents/upload for source_id: {TEST_DOC_SOURCE_ID}")
        response = await http_client.post("/api/documents/upload", data=api_form_data)

        # Assert API call success
        assert response.status_code == 202, f"API call failed: {response.status_code} - {response.text}"
        response_data = response.json()
        assert "document_id" in response_data
        assert response_data.get("task_enqueued") is True
        document_db_id = response_data["document_id"]
        logger.info(f"API call successful. Document DB ID: {document_db_id}")

        # --- Act: Fetch the Task ID ---
        # Need to query the DB to find the task enqueued by the API call
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            # Wait briefly for task to likely appear in DB after API commit
            await asyncio.sleep(0.2)
            select_task_stmt = select(tasks_table.c.task_id).where(
                # Filter by task type and payload content (document_id)
                tasks_table.c.task_type == "process_uploaded_document",
                # Note: JSON operators might be DB specific (e.g., ->> for postgres)
                # Using LIKE for simplicity, adjust if needed for robustness
                tasks_table.c.payload.cast(sqlalchemy.Text).like(f'%\"document_id\": {document_db_id}%')
            ).order_by(tasks_table.c.created_at.desc()).limit(1) # Get the latest matching task

            task_info = await db.fetch_one(select_task_stmt)
            assert task_info is not None, f"Could not find enqueued task for document ID {document_db_id}"
            indexing_task_id = task_info["task_id"]
            logger.info(f"Found indexing task ID: {indexing_task_id} for document DB ID: {document_db_id}")

        # --- Act: Wait for Indexing Task Completion ---
        # Signal the worker (in case it was waiting) - API doesn't pass the event,
        # but worker polls periodically anyway. Setting it ensures faster pickup if needed.
        test_new_task_event.set()
        logger.info(f"Waiting for task {indexing_task_id} to complete...")
        await wait_for_tasks_to_complete(
             pg_vector_db_engine, task_ids={indexing_task_id}, timeout_seconds=20.0 # Increased timeout slightly
        )
        logger.info(f"Task {indexing_task_id} reported as complete.")


        # --- Assertions (Remain the same as before) ---

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
        found_semantic_result = next((r for r in semantic_query_results if r.get("source_id") == TEST_DOC_SOURCE_ID), None)
        assert found_semantic_result is not None, f"Ingested document (Source ID: {TEST_DOC_SOURCE_ID}) not found in semantic query results: {semantic_query_results}"
        logger.info(f"Found matching semantic result: {found_semantic_result}")
        assert "distance" in found_semantic_result, "Semantic result missing 'distance' field"
        assert found_semantic_result["distance"] < 0.1, f"Semantic distance should be small, but was {found_semantic_result['distance']}"
        assert found_semantic_result.get("embedding_type") == "content_chunk"
        assert found_semantic_result.get("embedding_source_content") == TEST_DOC_CHUNK_1
        assert found_semantic_result.get("title") == TEST_DOC_TITLE
        assert found_semantic_result.get("source_type") == TEST_DOC_SOURCE_TYPE
        # Metadata comes back as string from JSONB sometimes, compare parsed dicts
        assert found_semantic_result.get("doc_metadata") == TEST_DOC_METADATA

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
        found_keyword_result = next((r for r in keyword_query_results if r.get("source_id") == TEST_DOC_SOURCE_ID), None)
        assert found_keyword_result is not None, f"Ingested document (Source ID: {TEST_DOC_SOURCE_ID}) not found in keyword query results: {keyword_query_results}"
        logger.info(f"Found matching keyword result: {found_keyword_result}")
        assert "rrf_score" in found_keyword_result, "Keyword result missing 'rrf_score' field"
        assert "fts_score" in found_keyword_result, "Keyword result missing 'fts_score' field"
        assert found_keyword_result["fts_score"] > 0, f"Keyword FTS score should be positive, but was {found_keyword_result['fts_score']}"
        assert found_keyword_result.get("embedding_type") in ["title", "content_chunk"]
        if found_keyword_result.get("embedding_type") == "title":
            assert found_keyword_result.get("embedding_source_content") == TEST_DOC_TITLE
        else: # content_chunk
            assert found_keyword_result.get("embedding_source_content") == TEST_DOC_CHUNK_0
        assert found_keyword_result.get("title") == TEST_DOC_TITLE
        assert found_keyword_result.get("source_type") == TEST_DOC_SOURCE_TYPE

        logger.info("--- Document Indexing E2E Test via API Passed ---")

    finally:
        # --- Cleanup ---
        # Stop the worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} stopped.")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
            worker_task.cancel()
            try: await worker_task
            except asyncio.CancelledError: logger.info(f"Worker task {worker_id} cancellation confirmed.")
        except Exception as e:
             logger.error(f"Error stopping worker task {worker_id}: {e}", exc_info=True)

        # Clean up document and task
        if document_db_id:
             try:
                 async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                     await storage.delete_document(db_cleanup, document_db_id)
                     logger.info(f"Cleaned up test document DB ID {document_db_id}")
             except Exception as cleanup_err:
                 logger.warning(f"Error during test document cleanup: {cleanup_err}")
        # Task should be 'done' or 'failed', but delete entry if needed
        if indexing_task_id:
             try:
                 async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                     delete_stmt = tasks_table.delete().where(tasks_table.c.task_id == indexing_task_id)
                     await db_cleanup.execute_with_retry(delete_stmt)
                     logger.info(f"Cleaned up test task ID {indexing_task_id}")
             except Exception as cleanup_err:
                 logger.warning(f"Error during test task cleanup: {cleanup_err}")
