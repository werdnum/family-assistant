"""
End-to-end functional tests for the email indexing and vector search pipeline.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy import select

# Import components needed for the E2E test
from family_assistant import storage, task_worker
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.email_indexer import (
    handle_index_email,
    set_indexing_dependencies,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table, store_incoming_email
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import query_vectors

# Import test helpers
from tests.helpers import wait_for_tasks_to_complete

logger = logging.getLogger(__name__)

# --- Test Configuration ---
# Use constants consistent with test_vector_storage if applicable, or define new ones
TEST_EMBEDDING_MODEL = "mock-e2e-email-model"
TEST_EMBEDDING_DIMENSION = 128  # Smaller dimension for mock testing


# --- Test Data for E2E ---
TEST_EMAIL_SUBJECT = "E2E Test: Project Alpha Kickoff Meeting"
TEST_EMAIL_BODY = "This email confirms the Project Alpha kickoff meeting scheduled for next Tuesday. Please find the agenda attached."
TEST_EMAIL_SENDER = "project.manager@example.com"
TEST_EMAIL_RECIPIENT = "team.inbox@example.com"
TEST_EMAIL_MESSAGE_ID = f"<e2e_email_{uuid.uuid4()}@example.com>"

# Sample form data mimicking Mailgun webhook payload
TEST_EMAIL_FORM_DATA = {
    "subject": TEST_EMAIL_SUBJECT,
    "stripped-text": TEST_EMAIL_BODY,
    "sender": TEST_EMAIL_SENDER,
    "recipient": TEST_EMAIL_RECIPIENT,
    "Message-Id": TEST_EMAIL_MESSAGE_ID,
    "From": f"Project Manager <{TEST_EMAIL_SENDER}>",
    "To": f"Team Inbox <{TEST_EMAIL_RECIPIENT}>",
    "Date": datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S %z"
    ),  # RFC 2822 format
    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
    "token": "dummy_token_e2e",
    "signature": "dummy_signature_e2e",
    "message-headers": f'[["Subject", "{TEST_EMAIL_SUBJECT}"], ["From", "Project Manager <{TEST_EMAIL_SENDER}>"], ["To", "Team Inbox <{TEST_EMAIL_RECIPIENT}>"]]',  # Simplified headers
}

TEST_QUERY_TEXT = "meeting about Project Alpha"  # Text relevant to the subject/body


@pytest.mark.asyncio
async def test_email_indexing_and_query_e2e(pg_vector_db_engine):
    """
    End-to-end test for email ingestion, indexing via task worker, and vector query retrieval.
    1. Setup Mock Embedder.
    2. Inject mock embedder.
    3. Register the 'index_email' task handler.
    4. Ingest a test email using store_incoming_email (which enqueues the task).
    5. Start the task worker loop in the background.
    6. Wait for the specific indexing task to complete using the helper.
    7. Stop the task worker loop.
    8. Generate a query embedding for relevant text.
    9. Execute query_vectors.
    10. Verify the ingested email is found in the results.
    """
    logger.info("\n--- Running Email Indexing E2E Test ---")

    # --- Arrange: Mock Embeddings ---
    # Create deterministic embeddings for known text parts
    title_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.1
    ).tolist()  # Different vectors
    body_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.2
    ).tolist()
    # Make query embedding closer to body embedding for relevance
    query_embedding = (np.array(body_embedding) + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01).tolist()


    embedding_map = {
        TEST_EMAIL_SUBJECT: title_embedding,
        TEST_EMAIL_BODY: body_embedding,
        TEST_QUERY_TEXT: query_embedding,
    }
    mock_embedder = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(), # Default if needed
    )

    # --- Arrange: Set Indexing Dependencies ---
    set_indexing_dependencies(embedding_generator=mock_embedder, llm_client=None)
    logger.info(f"Set mock embedding generator ({TEST_EMBEDDING_MODEL}) for indexing.")

    # --- Arrange: Register Task Handler ---
    # Ensure the handler is registered for the task worker loop
    # This might be redundant if already done in main.py setup, but safe to do here for test isolation
    task_worker.register_task_handler("index_email", handle_index_email)
    logger.info("Ensured 'index_email' task handler is registered.")


    email_db_id = None
    indexing_task_id = None

    # --- Act: Ingest Email (enqueues task) ---
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info(f"Ingesting test email with Message-ID: {TEST_EMAIL_MESSAGE_ID}")
        await store_incoming_email(db, form_data=TEST_EMAIL_FORM_DATA)

        # Fetch the email ID and task ID from the database
        select_email_stmt = select(
            received_emails_table.c.id, received_emails_table.c.indexing_task_id
        ).where(received_emails_table.c.message_id_header == TEST_EMAIL_MESSAGE_ID)
        email_info = await db.fetch_one(select_email_stmt)

        assert email_info is not None, "Failed to retrieve ingested email from DB"
        email_db_id = email_info["id"]
        indexing_task_id = email_info["indexing_task_id"]
        assert email_db_id is not None, "Email DB ID is null"
        assert indexing_task_id is not None, "Indexing Task ID is null"
        logger.info(
            f"Email ingested (DB ID: {email_db_id}), Indexing Task ID: {indexing_task_id}"
        )

    # --- Act: Run Task Worker and Wait ---
    worker_id = f"test-worker-{uuid.uuid4()}"
    # Use task_worker's events
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event() # Worker will wait on this

    # Replace global events temporarily for the test worker instance
    original_shutdown_event = task_worker.shutdown_event
    original_new_task_event = task_worker.new_task_event
    task_worker.shutdown_event = test_shutdown_event
    task_worker.new_task_event = test_new_task_event

    worker_task = None
    try:
        logger.info(f"Starting background task worker {worker_id}...")
        worker_task = asyncio.create_task(
            task_worker.task_worker_loop(worker_id, test_new_task_event)
        )
        # Give the worker a moment to start polling
        await asyncio.sleep(0.1)

        # Signal the worker there might be a new task (optional, but helps speed up pickup)
        test_new_task_event.set()

        logger.info(f"Waiting for task {indexing_task_id} to complete...")
        await wait_for_tasks_to_complete(
            pg_vector_db_engine, task_ids={indexing_task_id}, timeout_seconds=15.0
        )
        logger.info(f"Task {indexing_task_id} reported as complete.")

    finally:
        # Stop the worker and restore original events
        if worker_task:
            logger.info(f"Stopping background task worker {worker_id}...")
            test_shutdown_event.set()
            try:
                await asyncio.wait_for(worker_task, timeout=5.0)
                logger.info(f"Background task worker {worker_id} stopped.")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
                worker_task.cancel()
            except Exception as e:
                 logger.error(f"Error stopping worker task {worker_id}: {e}", exc_info=True)

        # Restore original events
        task_worker.shutdown_event = original_shutdown_event
        task_worker.new_task_event = original_new_task_event


    # --- Act: Query Vectors ---
    query_results = None
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info(f"Querying vectors using text: '{TEST_QUERY_TEXT}'")
        query_results = await query_vectors(
            db,
            query_embedding=query_embedding,  # Use the mock query embedding
            embedding_model=TEST_EMBEDDING_MODEL,  # Must match the mock model name
            limit=5,
            # Optional: Add filters if needed, e.g., source_type='email'
            filters={"source_type": "email"}
        )

    # --- Assert ---
    assert query_results is not None, "query_vectors returned None"
    assert len(query_results) > 0, "No results returned from vector query"
    logger.info(f"Query returned {len(query_results)} result(s).")

    # Find the result corresponding to our document
    found_result = None
    for result in query_results:
        # Check against the source_id (Message-ID) stored in the documents table
        if result.get("source_id") == TEST_EMAIL_MESSAGE_ID:
            found_result = result
            break

    assert (
        found_result is not None
    ), f"Ingested email (Source ID: {TEST_EMAIL_MESSAGE_ID}) not found in query results: {query_results}"
    logger.info(f"Found matching result: {found_result}")

    # Check distance (should be small since query embedding was close to body)
    assert "distance" in found_result, "Result missing 'distance' field"
    # Distance depends on the mock vectors, check it's reasonably small
    assert found_result["distance"] < 0.1, f"Distance should be small, but was {found_result['distance']}"

    # Check other fields in the result
    # The closest embedding might be title or body depending on the random vectors
    assert found_result.get("embedding_type") in ["content_chunk", "title"]
    if found_result.get("embedding_type") == "content_chunk":
        assert found_result.get("embedding_source_content") == TEST_EMAIL_BODY
    else:
         assert found_result.get("embedding_source_content") == TEST_EMAIL_SUBJECT

    assert found_result.get("title") == TEST_EMAIL_SUBJECT
    assert found_result.get("source_type") == "email"

    logger.info("--- Email Indexing E2E Test Passed ---")
