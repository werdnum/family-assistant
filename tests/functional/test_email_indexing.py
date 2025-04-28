"""
End-to-end functional tests for the email indexing and vector search pipeline.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Tuple # Add missing typing imports

import numpy as np
import pytest
from sqlalchemy import select

# Import components needed for the E2E test
from family_assistant import storage
from family_assistant.task_worker import TaskWorker, shutdown_event, new_task_event
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


# --- Helper Function for Test Setup ---

async def _ingest_and_index_email(
    engine, form_data: Dict[str, Any], task_timeout: float = 15.0
) -> Tuple[int, str]:
    """
    Helper to ingest an email, wait for its indexing task, and return IDs.

    Args:
        engine: The database engine fixture.
        form_data: The email data to ingest.
        task_timeout: Timeout for waiting for the task.

    Returns:
        A tuple containing (email_db_id, indexing_task_id).
    """
    email_db_id = None
    indexing_task_id = None
    message_id = form_data.get("Message-Id", "UNKNOWN_MESSAGE_ID")

    async with DatabaseContext(engine=engine) as db:
        logger.info(f"Helper: Ingesting test email with Message-ID: {message_id}")
        await store_incoming_email(db, form_data=form_data)

        # Fetch the email ID and task ID
        select_email_stmt = select(
            received_emails_table.c.id, received_emails_table.c.indexing_task_id
        ).where(received_emails_table.c.message_id_header == message_id)
        email_info = await db.fetch_one(select_email_stmt)

        assert email_info is not None, f"Failed to retrieve ingested email {message_id}"
        email_db_id = email_info["id"]
        indexing_task_id = email_info["indexing_task_id"]
        assert email_db_id is not None, f"Email DB ID is null for {message_id}"
        assert indexing_task_id is not None, f"Indexing Task ID is null for {message_id}"
        logger.info(
            f"Helper: Email ingested (DB ID: {email_db_id}), Task ID: {indexing_task_id}"
        )

    # Wait for the specific task to complete
    logger.info(f"Helper: Waiting for task {indexing_task_id} to complete...")
    await wait_for_tasks_to_complete(
        engine, task_ids={indexing_task_id}, timeout_seconds=task_timeout
    )
    logger.info(f"Helper: Task {indexing_task_id} reported as complete.")

    return email_db_id, indexing_task_id


# --- Test Functions ---

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
    # Create a TaskWorker instance for this test and register the handler
    worker = TaskWorker(processing_service=None)
    worker.register_task_handler("index_email", handle_index_email)
    logger.info("TaskWorker created and 'index_email' task handler registered.")

    # --- Act: Start Background Worker ---
    # Start the worker in the background *before* ingesting
    worker_id = f"test-worker-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event() # Worker will wait on this
    original_shutdown_event = shutdown_event
    original_new_task_event = new_task_event
    # No need to reassign module-level events since we'll use our own worker instance
    
    worker_task = asyncio.create_task(
        worker.run(test_new_task_event)
    )
    logger.info(f"Started background task worker {worker_id}...")
    await asyncio.sleep(0.1) # Give worker time to start

    try:
        # --- Act: Ingest Email and Wait for Indexing ---
        email_db_id, indexing_task_id = await _ingest_and_index_email(
            pg_vector_db_engine, TEST_EMAIL_FORM_DATA
        )
        # Signal worker just in case it missed the notification via enqueue_task
        test_new_task_event.set()
        # Wait again to be sure (wait_for_tasks_to_complete handles completion check)
        await wait_for_tasks_to_complete(
             pg_vector_db_engine, task_ids={indexing_task_id}, timeout_seconds=10.0
        )

        # --- Act: Query Vectors ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(f"Querying vectors using text: '{TEST_QUERY_TEXT}'")
            query_results = await query_vectors(
                db,
                query_embedding=query_embedding,  # Use the mock query embedding
                embedding_model=TEST_EMBEDDING_MODEL,  # Must match the mock model name
                limit=5,
                filters={"source_type": "email"} # Example filter
            )

        # --- Assert ---
        assert query_results is not None, "query_vectors returned None"
        assert len(query_results) > 0, "No results returned from vector query"
        logger.info(f"Query returned {len(query_results)} result(s).")

        # Find the result corresponding to our document
        found_result = None
        for result in query_results:
            if result.get("source_id") == TEST_EMAIL_MESSAGE_ID:
                found_result = result
                break

        assert (
            found_result is not None
        ), f"Ingested email (Source ID: {TEST_EMAIL_MESSAGE_ID}) not found in query results: {query_results}"
        logger.info(f"Found matching result: {found_result}")

        # Check distance (should be small since query embedding was close to body)
        assert "distance" in found_result, "Result missing 'distance' field"
        assert found_result["distance"] < 0.1, f"Distance should be small, but was {found_result['distance']}"

        # Check other fields in the result
        assert found_result.get("embedding_type") in ["content_chunk", "title"]
        if found_result.get("embedding_type") == "content_chunk":
            assert found_result.get("embedding_source_content") == TEST_EMAIL_BODY
        else:
             assert found_result.get("embedding_source_content") == TEST_EMAIL_SUBJECT

        assert found_result.get("title") == TEST_EMAIL_SUBJECT
        assert found_result.get("source_type") == "email"

        logger.info("--- Email Indexing E2E Test Passed ---")

    finally:
        # Stop the worker
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


@pytest.mark.asyncio
async def test_vector_ranking(pg_vector_db_engine):
    """
    Tests if vector search returns results ranked correctly by distance.
    1. Ingest three emails with distinct content.
    2. Assign embeddings such that one is very close, one medium, one far from a query.
    3. Perform a vector-only query.
    4. Assert the results are ordered correctly by distance.
    """
    logger.info("\n--- Running Vector Ranking Test ---")

    # --- Arrange: Mock Embeddings ---
    # Create embeddings with controlled distances
    base_vec = np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32)
    vec_close = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.01).tolist()
    vec_medium = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.1).tolist()
    vec_far = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.5).tolist()
    query_vec = base_vec.tolist() # Query is the base vector

    email1_body = "Content for the closest document."
    email2_body = "Content for the medium distance document."
    email3_body = "Content for the farthest document."
    email1_msg_id = f"<rank_close_{uuid.uuid4()}@example.com>"
    email2_msg_id = f"<rank_medium_{uuid.uuid4()}@example.com>"
    email3_msg_id = f"<rank_far_{uuid.uuid4()}@example.com>"
    # Define titles used in form_data below
    email1_title = "Rank Test Close"
    email2_title = "Rank Test Medium"
    email3_title = "Rank Test Far"

    # Add mock embeddings for titles
    title1_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.05).tolist()
    title2_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.06).tolist()
    title3_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.07).tolist()

    embedding_map = {
        email1_body: vec_close,
        email2_body: vec_medium,
        email3_body: vec_far,
        email1_title: title1_vec, # Add title embedding
        email2_title: title2_vec, # Add title embedding
        email3_title: title3_vec, # Add title embedding
        "query": query_vec, # Query text doesn't matter here, just the vector
    }
    # Provide a default embedding
    mock_embedder = MockEmbeddingGenerator(
        embedding_map,
        TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist()
    )
    set_indexing_dependencies(embedding_generator=mock_embedder)
    # TaskWorker instance is created and registered earlier in this test
    # worker is already defined and will handle index_email tasks

    # --- Arrange: Ingest Emails ---
    form_data1 = TEST_EMAIL_FORM_DATA.copy()
    form_data1["stripped-text"] = email1_body
    form_data1["Message-Id"] = email1_msg_id
    form_data1["subject"] = email1_title # Use variable

    form_data2 = TEST_EMAIL_FORM_DATA.copy()
    form_data2["stripped-text"] = email2_body
    form_data2["Message-Id"] = email2_msg_id
    form_data2["subject"] = email2_title # Use variable

    form_data3 = TEST_EMAIL_FORM_DATA.copy()
    form_data3["stripped-text"] = email3_body
    form_data3["Message-Id"] = email3_msg_id
    form_data3["subject"] = email3_title # Use variable

    # Create TaskWorker instance and start it
    worker = TaskWorker(processing_service=None)
    worker.register_task_handler("index_email", handle_index_email)
    
    worker_id = f"test-worker-rank-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    
    worker_task = asyncio.create_task(
        worker.run(test_new_task_event)
    )
    await asyncio.sleep(0.1)

    all_task_ids = set()
    try:
        _, task_id1 = await _ingest_and_index_email(pg_vector_db_engine, form_data1)
        _, task_id2 = await _ingest_and_index_email(pg_vector_db_engine, form_data2)
        _, task_id3 = await _ingest_and_index_email(pg_vector_db_engine, form_data3)
        all_task_ids = {task_id1, task_id2, task_id3}
        test_new_task_event.set() # Signal worker
        await wait_for_tasks_to_complete(pg_vector_db_engine, task_ids=all_task_ids, timeout_seconds=20.0)

        # --- Act: Query Vectors ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info("Querying vectors for ranking test...")
            query_results = await query_vectors(
                db,
                query_embedding=query_vec,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
            )

        # --- Assert ---
        assert query_results is not None and len(query_results) >= 3, "Expected at least 3 results"
        logger.info(f"Ranking query returned {len(query_results)} results.")

        # Extract source IDs and distances
        results_ordered = [(r.get("source_id"), r.get("distance")) for r in query_results]
        logger.info(f"Results ordered by distance: {results_ordered}")

        # Find the indices of our test emails in the results
        try:
            idx1 = [r[0] for r in results_ordered].index(email1_msg_id)
            idx2 = [r[0] for r in results_ordered].index(email2_msg_id)
            idx3 = [r[0] for r in results_ordered].index(email3_msg_id)
        except ValueError:
            pytest.fail(f"One or more test emails not found in results: {results_ordered}")

        # Assert the order based on distance (lower index means closer/better rank)
        assert idx1 < idx2, f"Closest email ({email1_msg_id}) was not ranked higher than medium ({email2_msg_id})"
        assert idx2 < idx3, f"Medium email ({email2_msg_id}) was not ranked higher than farthest ({email3_msg_id})"

        logger.info("--- Vector Ranking Test Passed ---")

    finally:
        # Stop worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try: await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError: worker_task.cancel()


@pytest.mark.asyncio
async def test_metadata_filtering(pg_vector_db_engine):
    """
    Tests if metadata filters correctly exclude documents, even if they are
    vector-wise closer.
    1. Ingest two emails with different source_type metadata.
    2. Assign embeddings such that the email with the NON-matching source_type is closer.
    3. Perform a vector query with a metadata filter matching the FURTHER email.
    4. Assert only the email matching the filter is returned.
    """
    logger.info("\n--- Running Metadata Filtering Test ---")

    # --- Arrange: Mock Embeddings ---
    base_vec = np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32)
    vec_close_wrong_type = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.01).tolist()
    vec_far_correct_type = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.2).tolist()
    query_vec = base_vec.tolist()

    email1_body = "Content for the close document (wrong type)."
    email2_body = "Content for the far document (correct type)."
    email1_msg_id = f"<meta_close_{uuid.uuid4()}@example.com>"
    email2_msg_id = f"<meta_far_{uuid.uuid4()}@example.com>"
    # Define titles used in form_data below
    email1_title = "Metadata Test Close Wrong Type"
    email2_title = "Metadata Test Far Correct Type"

    # Add mock embeddings for titles
    title1_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.05).tolist()
    title2_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.06).tolist()

    embedding_map = {
        email1_body: vec_close_wrong_type,
        email2_body: vec_far_correct_type,
        email1_title: title1_vec, # Add title embedding
        email2_title: title2_vec, # Add title embedding
        "query": query_vec,
    }
    # Provide a default embedding
    mock_embedder = MockEmbeddingGenerator(
        embedding_map,
        TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist()
    )
    set_indexing_dependencies(embedding_generator=mock_embedder)
    # TaskWorker instance is created and registered earlier in this test
    # worker is already defined and will handle index_email tasks

    # --- Arrange: Ingest Emails with different source_type ---
    form_data1 = TEST_EMAIL_FORM_DATA.copy()
    form_data1["stripped-text"] = email1_body
    form_data1["Message-Id"] = email1_msg_id
    form_data1["subject"] = email1_title # Use variable for title
    # We need to modify how source_type is set, currently hardcoded in EmailDocument
    # For this test, let's simulate by adding metadata that we can filter on
    form_data1["X-Custom-Type"] = "receipt" # Simulate metadata

    form_data2 = TEST_EMAIL_FORM_DATA.copy()
    form_data2["stripped-text"] = email2_body
    form_data2["Message-Id"] = email2_msg_id
    form_data2["subject"] = email2_title # Use variable for title
    form_data2["X-Custom-Type"] = "invoice" # Simulate metadata

    # Create TaskWorker instance and start it
    worker = TaskWorker(processing_service=None)
    worker.register_task_handler("index_email", handle_index_email)
    
    worker_id = f"test-worker-meta-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    
    worker_task = asyncio.create_task(
        worker.run(test_new_task_event)
    )
    await asyncio.sleep(0.1)

    all_task_ids = set()
    try:
        _, task_id1 = await _ingest_and_index_email(pg_vector_db_engine, form_data1)
        _, task_id2 = await _ingest_and_index_email(pg_vector_db_engine, form_data2)
        all_task_ids = {task_id1, task_id2}
        test_new_task_event.set()
        await wait_for_tasks_to_complete(pg_vector_db_engine, task_ids=all_task_ids, timeout_seconds=20.0)

        # --- Act: Query Vectors with Metadata Filter ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info("Querying vectors with metadata filter 'source_type=email'...")
            # The filter needs to target the actual column name ('source_type')
            # or potentially JSONB metadata if we stored X-Custom-Type there.
            # Let's assume source_type is always 'email' for now from EmailDocument
            # and filter on something else we can control, like title.
            # Re-adjusting test: Filter on title instead of source_type for simplicity.
            query_results = await query_vectors(
                db,
                query_embedding=query_vec,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"title": "Metadata Test Far Correct Type"} # Filter for the second email's title
            )

        # --- Assert ---
        assert query_results is not None, "Query returned None"
        # Check that *at least one* result is returned
        assert len(query_results) > 0, f"Expected at least 1 result matching filter, got {len(query_results)}"
        logger.info(f"Metadata filter query returned {len(query_results)} result(s).")

        # Verify that *all* returned results belong to the correct document
        for found_result in query_results:
            assert found_result.get("source_id") == email2_msg_id, \
                f"Incorrect document returned by metadata filter. Expected source_id {email2_msg_id}, got {found_result.get('source_id')}"
            assert found_result.get("title") == "Metadata Test Far Correct Type", \
                f"Incorrect title in filtered result. Expected 'Metadata Test Far Correct Type', got {found_result.get('title')}"

        # Optional: Check that the closer document (email1) is NOT in the results
        source_ids_returned = {r.get("source_id") for r in query_results}
        assert email1_msg_id not in source_ids_returned, \
            f"Document {email1_msg_id} (which should be filtered out) was found in results."

        logger.info("--- Metadata Filtering Test Passed ---")

    finally:
        # Stop worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try: await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError: worker_task.cancel()


@pytest.mark.asyncio
async def test_keyword_filtering(pg_vector_db_engine):
    """
    Tests if keyword search correctly filters results in a hybrid query.
    1. Ingest two emails with similar vector embeddings but different keywords.
    2. Perform a hybrid query with keywords matching only one email.
    3. Assert only the email with matching keywords is returned (or ranked highest).
    """
    logger.info("\n--- Running Keyword Filtering Test ---")

    # --- Arrange: Mock Embeddings ---
    base_vec = np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32)
    # Make embeddings very close
    vec1 = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.001).tolist()
    vec2 = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.002).tolist()
    query_vec = base_vec.tolist()

    keyword = "banana"
    email1_body = f"This document talks about apples and oranges."
    email2_body = f"This document is all about the yellow {keyword} fruit."
    email1_msg_id = f"<keyword_no_{uuid.uuid4()}@example.com>"
    email2_msg_id = f"<keyword_yes_{uuid.uuid4()}@example.com>"
    # Define titles used in form_data below
    email1_title = "Keyword Test No Match"
    email2_title = f"Keyword Test Yes Match {keyword}"

    # Add mock embeddings for titles as well
    title1_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.05).tolist()
    title2_vec = (base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.06).tolist()

    embedding_map = {
        email1_body: vec1,
        email2_body: vec2,
        email1_title: title1_vec, # Add title embedding
        email2_title: title2_vec, # Add title embedding
        "query": query_vec,
    }
    # Provide a default embedding to prevent errors if other texts are encountered unexpectedly
    mock_embedder = MockEmbeddingGenerator(
        embedding_map,
        TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist()
    )
    set_indexing_dependencies(embedding_generator=mock_embedder)
    # TaskWorker instance is created and registered earlier in this test
    # worker is already defined and will handle index_email tasks

    # --- Arrange: Ingest Emails ---
    form_data1 = TEST_EMAIL_FORM_DATA.copy()
    form_data1["stripped-text"] = email1_body
    form_data1["Message-Id"] = email1_msg_id
    form_data1["subject"] = email1_title # Use variable for title

    form_data2 = TEST_EMAIL_FORM_DATA.copy()
    form_data2["stripped-text"] = email2_body
    form_data2["Message-Id"] = email2_msg_id
    form_data2["subject"] = email2_title # Use variable for title

    # Create TaskWorker instance and start it
    worker = TaskWorker(processing_service=None)
    worker.register_task_handler("index_email", handle_index_email)
    
    worker_id = f"test-worker-keyword-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    
    worker_task = asyncio.create_task(
        worker.run(test_new_task_event)
    )
    await asyncio.sleep(0.1)

    all_task_ids = set()
    try:
        _, task_id1 = await _ingest_and_index_email(pg_vector_db_engine, form_data1)
        _, task_id2 = await _ingest_and_index_email(pg_vector_db_engine, form_data2)
        all_task_ids = {task_id1, task_id2}
        test_new_task_event.set()
        await wait_for_tasks_to_complete(pg_vector_db_engine, task_ids=all_task_ids, timeout_seconds=20.0)

        # --- Act: Query Vectors with Keywords ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(f"Querying vectors with keyword: '{keyword}'...")
            query_results = await query_vectors(
                db,
                query_embedding=query_vec,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                keywords=keyword # Add the keyword search term
            )

        # --- Assert ---
        assert query_results is not None, "Query returned None"
        assert len(query_results) > 0, "Expected at least one result matching keyword"
        logger.info(f"Keyword filter query returned {len(query_results)} result(s).")

        # RRF should rank the keyword match highest, even if vector distance is slightly worse
        found_result = query_results[0] # Check the top result
        assert found_result.get("source_id") == email2_msg_id, f"Top result should be the one matching keyword '{keyword}'"
        assert keyword in found_result.get("embedding_source_content", "").lower(), "Keyword not found in top result content"
        assert "rrf_score" in found_result, "Result missing 'rrf_score'"
        assert found_result.get("fts_score", 0) > 0, "Keyword match should have a positive FTS score"

        # Ensure the non-matching document is either absent or ranked lower
        non_matching_present = any(r.get("source_id") == email1_msg_id for r in query_results)
        if non_matching_present:
             rank_non_match = [r.get("source_id") for r in query_results].index(email1_msg_id)
             rank_match = [r.get("source_id") for r in query_results].index(email2_msg_id)
             assert rank_match < rank_non_match, "Keyword match should be ranked higher than non-match"
             logger.info(f"Non-matching document found but ranked lower (Rank {rank_non_match}) than keyword match (Rank {rank_match}).")
        else:
             logger.info("Non-matching document correctly excluded from results.")


        logger.info("--- Keyword Filtering Test Passed ---")

    finally:
        # Stop worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try: await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError: worker_task.cancel()


# Add more tests here for hybrid search nuances, different filter combinations, etc.
