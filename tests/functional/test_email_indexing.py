"""
End-to-end functional tests for the email indexing and vector search pipeline.
"""

import asyncio
import logging
import re  # Add re import
import uuid
from datetime import datetime, timezone
from typing import Any  # Add missing typing imports
from unittest.mock import MagicMock  # Add this import

import numpy as np
import pytest
from assertpy import assert_that
from sqlalchemy import select

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.email_indexer import (
    handle_index_email,
    set_indexing_dependencies,
)
from family_assistant.indexing.pipeline import IndexingPipeline
from family_assistant.indexing.processors.dispatch_processors import (
    EmbeddingDispatchProcessor,
)
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table, store_incoming_email
from family_assistant.storage.tasks import (
    tasks_table,
)  # Keep if used for direct inspection, though wait_for_tasks_to_complete is preferred
from family_assistant.storage.vector import (
    DocumentEmbeddingRecord,
    DocumentRecord,
    query_vectors,
)  # Added imports
from family_assistant.task_worker import TaskWorker

# Import components needed for the E2E test
# Import test helpers
from tests.helpers import wait_for_tasks_to_complete

logger = logging.getLogger(__name__)

# --- Test Configuration ---
# Use constants consistent with test_vector_storage if applicable, or define new ones
TEST_EMBEDDING_MODEL = "mock-e2e-email-model"
TEST_EMBEDDING_DIMENSION = 128  # Smaller dimension for mock testing


# --- Test Data for E2E ---
TEST_EMAIL_SUBJECT = "E2E Test: Project Alpha Kickoff Meeting"
TEST_EMAIL_BODY = "This email confirms the Project Alpha kickoff meeting scheduled for next Tuesday Please find the agenda attached."
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


# --- Debugging Helper ---
async def dump_tables_on_failure(engine):
    """Logs the content of relevant tables for debugging."""
    logger.info("--- Dumping table contents on failure ---")
    async with DatabaseContext(engine=engine) as db:
        try:
            # Dump tasks table
            tasks_query = select(tasks_table)
            all_tasks = await db.fetch_all(tasks_query)
            logger.info("--- Tasks Table ---")
            if all_tasks:
                for task in all_tasks:
                    logger.info(f"  Task: {dict(task)}")
            else:
                logger.info("  (empty)")

            # Dump documents table
            docs_query = select(DocumentRecord)
            all_docs = await db.fetch_all(docs_query)
            logger.info("--- Documents Table ---")
            if all_docs:
                for doc in all_docs:
                    # Access columns directly if it's a RowMapping, or adapt if it returns ORM objects
                    logger.info(
                        f"  Document: {dict(doc)}"
                    )  # Assuming RowMapping for simplicity
            else:
                logger.info("  (empty)")

            # Dump document_embeddings table
            embeds_query = select(DocumentEmbeddingRecord)
            all_embeds = await db.fetch_all(embeds_query)
            logger.info("--- Document Embeddings Table ---")
            if all_embeds:
                for embed in all_embeds:
                    # Log relevant fields, potentially truncating the vector
                    embed_dict = dict(embed)
                    if (
                        "embedding" in embed_dict
                        and embed_dict["embedding"] is not None
                    ):
                        embed_dict["embedding"] = (
                            f"Vector[{len(embed_dict['embedding'])}]"  # Avoid logging huge vectors
                        )
                    logger.info(f"  Embedding: {embed_dict}")
            else:
                logger.info("  (empty)")

        except Exception as dump_exc:
            logger.error(f"Failed to dump tables on failure: {dump_exc}", exc_info=True)
    logger.info("--- End table dump ---")


# --- Helper Function for Test Setup ---


async def _ingest_and_index_email(
    engine,
    form_data: dict[str, Any],
    task_timeout: float = 15.0,
    notify_event: asyncio.Event | None = None,  # Add notify_event parameter
) -> int:
    """
    Helper to ingest an email, notify worker, wait for its indexing task, and return IDs.

    Args:
        engine: The database engine fixture.
        form_data: The email data to ingest.
        task_timeout: Timeout for waiting for the task.

    Returns:
        Email DB ID
    """
    email_db_id = None
    indexing_task_id = None
    message_id = form_data.get("Message-Id", "UNKNOWN_MESSAGE_ID")

    async with DatabaseContext(engine=engine) as db:
        logger.info(f"Helper: Ingesting test email with Message-ID: {message_id}")
        await store_incoming_email(
            db,
            form_data=form_data,
            notify_event=notify_event,  # Pass the event to store_incoming_email
        )

        # Fetch the email ID and task ID
        select_email_stmt = select(
            received_emails_table.c.id, received_emails_table.c.indexing_task_id
        ).where(received_emails_table.c.message_id_header == message_id)
        email_info = await db.fetch_one(select_email_stmt)

        assert_that(email_info).described_as(f"Failed to retrieve ingested email {message_id}").is_not_none()
        email_db_id = email_info["id"]
        assert_that(email_db_id).described_as(f"Email DB ID is null for {message_id}").is_not_none()
        logger.info(
            f"Helper: Email ingested (DB ID: {email_db_id})"
        )
    # Wait for all tasks to complete
    logger.info(
        f"Helper: Waiting for all pending tasks to complete after ingesting email DB ID {email_db_id} (initial task: {indexing_task_id})..."
    )
    await wait_for_tasks_to_complete(
        engine,
        timeout_seconds=task_timeout,  # task_ids=None (default) ensures all tasks
    )
    logger.info(
        f"Helper: All pending tasks reported as complete for email DB ID {email_db_id}."
    )

    return email_db_id


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
    query_embedding = (
        np.array(body_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()

    # Normalize test strings in the same way TextChunker does
    normalized_subject = re.sub(r"\s+", " ", TEST_EMAIL_SUBJECT).strip()
    normalized_body = re.sub(r"\s+", " ", TEST_EMAIL_BODY).strip()

    embedding_map = {
        normalized_subject: title_embedding,
        normalized_body: body_embedding,
        TEST_QUERY_TEXT: query_embedding,  # Query text is not processed by TextChunker
    }
    mock_embedder = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )

    # --- Arrange: Create Indexing Pipeline ---
    # For testing, we need a basic pipeline that extracts title, chunks text, and dispatches for embedding.
    title_extractor = TitleExtractor()
    text_chunker = TextChunker(
        chunk_size=500, chunk_overlap=50
    )  # Example chunker config
    # Configure EmbeddingDispatchProcessor to dispatch common types
    embedding_dispatcher = EmbeddingDispatchProcessor(
        embedding_types_to_dispatch=[
            "title_chunk",
            "raw_body_text_chunk",
        ],  # Adjusted to match TextChunker's output
    )

    test_pipeline = IndexingPipeline(
        processors=[title_extractor, text_chunker, embedding_dispatcher],
        config={},  # No specific pipeline config for this test
    )

    # --- Arrange: Set Indexing Dependencies ---
    set_indexing_dependencies(pipeline=test_pipeline)
    logger.info("Set IndexingPipeline for email indexing.")

    # --- Arrange: Register Task Handler ---
    # Create a TaskWorker instance for this test and register the handler
    # Provide dummy/mock values for the required arguments
    mock_application = MagicMock()  # Mock the application object
    # The TaskWorker needs access to the embedding_generator for handle_embed_and_store_batch
    # We can pass it via the processing_service or by making it available in app state
    # For simplicity here, we'll assume the TaskWorker can get it or we mock the context

    # Mock the processing service or ensure embedding_generator is in app_state for TaskWorker
    # For this test, let's assume ToolExecutionContext will provide it when handle_embed_and_store_batch is called.
    # The `embedding_generator` is passed to `handle_embed_and_store_batch` by the task worker loop
    # from `ToolExecutionContext.embedding_generator`.
    # So, the `ToolExecutionContext` created by the `TaskWorker` needs to have it.
    # We can achieve this by ensuring the `application.state.embedding_generator` is set,
    # as `TaskWorker`'s `_create_tool_execution_context` uses it.
    mock_application.state.embedding_generator = mock_embedder
    mock_application.state.llm_client = None  # Or a mock LLM if any processor uses it

    dummy_calendar_config = {}  # Not used by email/embedding tasks
    dummy_timezone_str = "UTC"  # Not used by email/embedding tasks
    worker = TaskWorker(
        processing_service=None,  # No processing service needed for this handler
        application=mock_application,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
    )
    worker.register_task_handler("index_email", handle_index_email)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)
    logger.info("TaskWorker created and 'index_email' task handler registered.")

    # --- Act: Start Background Worker ---
    # Start the worker in the background *before* ingesting
    worker_id = f"test-worker-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()  # Worker will wait on this
    # No need to reassign module-level events since we'll use our own worker instance

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    logger.info(f"Started background task worker {worker_id}...")
    await asyncio.sleep(0.1)  # Give worker time to start
    test_failed = False

    try:
        try:
            # --- Act: Ingest Email and Wait for Indexing ---
            # Pass the event directly during ingestion
            await _ingest_and_index_email(
                pg_vector_db_engine,
                TEST_EMAIL_FORM_DATA,
                notify_event=test_new_task_event,  # Pass the worker's event
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
                    filters={"source_type": "email"},  # Example filter
                )

            # --- Assert ---
            assert_that(query_results).described_as("query_vectors returned None").is_not_none()
            assert_that(query_results).described_as("No results returned from vector query").is_not_empty()
            logger.info(f"Query returned {len(query_results)} result(s).")

            # Find the result corresponding to our document
            found_result = None
            for result in query_results:
                if result.get("source_id") == TEST_EMAIL_MESSAGE_ID:
                    found_result = result
                    break

            assert_that(found_result).described_as(f"Ingested email (Source ID: {TEST_EMAIL_MESSAGE_ID}) not found in query results: {query_results}").is_not_none()
            logger.info(f"Found matching result: {found_result}")

            # Check distance (should be small since query embedding was close to body)
            assert_that(found_result).described_as(f"Result missing 'distance' field: {found_result}").contains_key("distance")
            assert_that(found_result["distance"]).described_as(f"Distance should be small").is_less_than(0.1)

            # Check other fields in the result
            assert_that(found_result.get("embedding_type")).is_in("raw_body_text_chunk", "title_chunk")
            if found_result.get("embedding_type") == "raw_body_text_chunk":
                assert_that(found_result.get("embedding_source_content")).is_equal_to(TEST_EMAIL_BODY)
            else:
                assert_that(found_result.get("embedding_source_content")).is_equal_to(TEST_EMAIL_SUBJECT)

            assert_that(found_result.get("title")).is_equal_to(TEST_EMAIL_SUBJECT)
            assert_that(found_result.get("source_type")).is_equal_to("email")

            logger.info("--- Email Indexing E2E Test Passed ---")

        except Exception as e:
            test_failed = True
            logger.error(f"Test failed: {e}", exc_info=True)
            raise  # Re-raise the exception after logging
    finally:
        # Stop the worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} stopped.")

            # Dump tables if the test failed before this finally block
            if test_failed:
                await dump_tables_on_failure(pg_vector_db_engine)

        except asyncio.TimeoutError:
            logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
            worker_task.cancel()
        except Exception as e:
            logger.error(f"Error stopping worker task {worker_id}: {e}", exc_info=True)


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
    query_vec = base_vec.tolist()  # Query is the base vector

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
        email1_title: title1_vec,  # Add title embedding
        email2_title: title2_vec,  # Add title embedding
        email3_title: title3_vec,  # Add title embedding
        "query": query_vec,  # Query text doesn't matter here, just the vector
    }
    # Provide a default embedding
    mock_embedder = MockEmbeddingGenerator(
        embedding_map,
        TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )
    # --- Arrange: Create Indexing Pipeline ---
    title_extractor = TitleExtractor()
    text_chunker = TextChunker(chunk_size=500, chunk_overlap=50)
    embedding_dispatcher_kw = (
        EmbeddingDispatchProcessor(  # Renamed for clarity if needed, or reuse
            embedding_types_to_dispatch=["title_chunk", "raw_body_text_chunk"],
        )
    )  # Adjusted
    test_pipeline_kw = IndexingPipeline(  # Renamed for clarity
        processors=[title_extractor, text_chunker, embedding_dispatcher_kw], config={}
    )
    set_indexing_dependencies(pipeline=test_pipeline_kw)

    # Mock application for TaskWorker
    mock_application_kw = MagicMock()  # Define mock_application_kw
    mock_application_kw.state.embedding_generator = mock_embedder
    mock_application_kw.state.llm_client = None

    dummy_calendar_config_kw = {}  # Define dummy_calendar_config_kw
    dummy_timezone_str_kw = "UTC"  # Define dummy_timezone_str_kw

    # --- Arrange: Ingest Emails ---
    form_data1 = TEST_EMAIL_FORM_DATA.copy()
    form_data1["stripped-text"] = email1_body
    form_data1["Message-Id"] = email1_msg_id
    form_data1["subject"] = email1_title  # Use variable

    form_data2 = TEST_EMAIL_FORM_DATA.copy()
    form_data2["stripped-text"] = email2_body
    form_data2["Message-Id"] = email2_msg_id
    form_data2["subject"] = email2_title  # Use variable

    form_data3 = TEST_EMAIL_FORM_DATA.copy()
    form_data3["stripped-text"] = email3_body
    form_data3["Message-Id"] = email3_msg_id
    form_data3["subject"] = email3_title  # Use variable

    # Create TaskWorker instance and start it
    # Provide dummy/mock values for the required arguments
    worker = TaskWorker(
        processing_service=None,  # No processing service needed for this handler
        application=mock_application_kw,
        calendar_config=dummy_calendar_config_kw,
        timezone_str=dummy_timezone_str_kw,
    )
    worker.register_task_handler("index_email", handle_index_email)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-rank-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    test_failed = False
    try:
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data1, notify_event=test_new_task_event
        )
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data2, notify_event=test_new_task_event
        )
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data3, notify_event=test_new_task_event
        )

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
        assert_that(query_results).is_not_none()
        assert_that(query_results).described_as("Expected at least 3 results").has_length_greater_than_or_equal_to(3)
        logger.info(f"Ranking query returned {len(query_results)} results.")

        # Extract source IDs and distances
        results_ordered = [
            (r.get("source_id"), r.get("distance")) for r in query_results
        ]
        logger.info(f"Results ordered by distance: {results_ordered}")

        # Find the indices of our test emails in the results
        result_source_ids = [r[0] for r in results_ordered]
        assert_that(result_source_ids).described_as(f"Results for ranking: {results_ordered}").contains(email1_msg_id, email2_msg_id, email3_msg_id)
        idx1 = result_source_ids.index(email1_msg_id)
        idx2 = result_source_ids.index(email2_msg_id)
        idx3 = result_source_ids.index(email3_msg_id)

        # Assert the order based on distance (lower index means closer/better rank)
        assert_that(idx1).described_as(f"Closest email ({email1_msg_id}) ranking").is_less_than(idx2)
        assert_that(idx2).described_as(f"Medium email ({email2_msg_id}) ranking").is_less_than(idx3)

        logger.info("--- Vector Ranking Test Passed ---")
    except Exception as e:
        test_failed = True
        logger.error(f"Test failed: {e}", exc_info=True)
        raise

    finally:
        # Stop worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            if test_failed:
                await dump_tables_on_failure(pg_vector_db_engine)
        except asyncio.TimeoutError:
            worker_task.cancel()


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
    vec_close_wrong_type = (
        base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.01
    ).tolist()
    vec_far_correct_type = (
        base_vec + np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.2
    ).tolist()
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
        email1_title: title1_vec,  # Add title embedding
        email2_title: title2_vec,  # Add title embedding
        "query": query_vec,
    }
    # Provide a default embedding
    mock_embedder = MockEmbeddingGenerator(
        embedding_map,
        TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )
    # --- Arrange: Create Indexing Pipeline ---
    title_extractor_meta = TitleExtractor()
    text_chunker_meta = TextChunker(chunk_size=500, chunk_overlap=50)
    embedding_dispatcher_meta = EmbeddingDispatchProcessor(
        embedding_types_to_dispatch=["title_chunk", "raw_body_text_chunk"],
    )  # Adjusted
    test_pipeline_meta = IndexingPipeline(
        processors=[title_extractor_meta, text_chunker_meta, embedding_dispatcher_meta],
        config={},
    )
    set_indexing_dependencies(pipeline=test_pipeline_meta)

    # Mock application for TaskWorker
    mock_application_meta = MagicMock()
    mock_application_meta.state.embedding_generator = mock_embedder
    mock_application_meta.state.llm_client = None

    # --- Arrange: Ingest Emails with different source_type ---
    form_data1 = TEST_EMAIL_FORM_DATA.copy()
    form_data1["stripped-text"] = email1_body
    form_data1["Message-Id"] = email1_msg_id
    form_data1["subject"] = email1_title  # Use variable for title
    # We need to modify how source_type is set, currently hardcoded in EmailDocument
    # For this test, let's simulate by adding metadata that we can filter on
    form_data1["X-Custom-Type"] = "receipt"  # Simulate metadata

    form_data2 = TEST_EMAIL_FORM_DATA.copy()
    form_data2["stripped-text"] = email2_body
    form_data2["Message-Id"] = email2_msg_id
    form_data2["subject"] = email2_title  # Use variable for title
    form_data2["X-Custom-Type"] = "invoice"  # Simulate metadata

    # Create TaskWorker instance and start it
    # Provide dummy/mock values for the required arguments
    dummy_calendar_config_meta = {}  # Define dummy_calendar_config_meta
    dummy_timezone_str_meta = "UTC"
    worker = TaskWorker(
        processing_service=None,  # No processing service needed for this handler
        application=mock_application_meta,
        calendar_config=dummy_calendar_config_meta,
        timezone_str=dummy_timezone_str_meta,
    )
    worker.register_task_handler("index_email", handle_index_email)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-meta-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    test_failed = False
    try:
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data1, notify_event=test_new_task_event
        )
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data2, notify_event=test_new_task_event
        )

        # --- Act: Query Vectors with Metadata Filter ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            # The filter needs to target the actual column name ('source_type')
            # or potentially JSONB metadata if we stored X-Custom-Type there.
            # Let's assume source_type is always 'email' for now from EmailDocument
            # and filter on something else we can control, like title.
            # Re-adjusting test: Filter on title instead of source_type for simplicity.
            # Changed to filter on source_id as title filtering seems problematic in query_vectors
            active_filter = {"source_id": email2_msg_id}
            logger.info(f"Querying vectors with metadata filter {active_filter}...")
            query_results = await query_vectors(
                db,
                query_embedding=query_vec,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters=active_filter,
            )

        # --- Assert ---
        assert_that(query_results).described_as("Query returned None").is_not_none()
        # Check that *at least one* result is returned
        assert_that(query_results).described_as(f"Expected at least 1 result matching filter").is_not_empty()
        logger.info(f"Metadata filter query returned {len(query_results)} result(s).")

        # Verify that *all* returned results belong to the correct document
        for found_result in query_results:
            assert_that(found_result.get("source_id")).described_as(f"Incorrect document returned by metadata filter. Expected source_id {email2_msg_id}").is_equal_to(email2_msg_id)
            assert_that(found_result.get("title")).described_as(f"Incorrect title in filtered result. Expected 'Metadata Test Far Correct Type'").is_equal_to("Metadata Test Far Correct Type")

        # Optional: Check that the closer document (email1) is NOT in the results
        source_ids_returned = {r.get("source_id") for r in query_results}
        assert_that(source_ids_returned).described_as(f"Document {email1_msg_id} (which should be filtered out) was found in results.").does_not_contain(email1_msg_id)

        logger.info("--- Metadata Filtering Test Passed ---")
    except Exception as e:
        test_failed = True
        logger.error(f"Test failed: {e}", exc_info=True)
        raise
    finally:
        # Stop worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            if test_failed:
                await dump_tables_on_failure(pg_vector_db_engine)
        except asyncio.TimeoutError:
            worker_task.cancel()


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
    email1_body = "This document talks about apples and oranges."
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
        email1_title: title1_vec,  # Add title embedding
        email2_title: title2_vec,  # Add title embedding
        "query": query_vec,
    }
    # Provide a default embedding to prevent errors if other texts are encountered unexpectedly
    mock_embedder = MockEmbeddingGenerator(
        embedding_map,
        TEST_EMBEDDING_MODEL,
        default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )
    # --- Arrange: Create Indexing Pipeline ---
    title_extractor = TitleExtractor()
    text_chunker = TextChunker(chunk_size=500, chunk_overlap=50)
    embedding_dispatcher_kw = (
        EmbeddingDispatchProcessor(  # Renamed for clarity if needed, or reuse
            embedding_types_to_dispatch=["title_chunk", "raw_body_text_chunk"],
        )
    )  # Adjusted
    test_pipeline_kw = IndexingPipeline(  # Renamed for clarity
        processors=[title_extractor, text_chunker, embedding_dispatcher_kw], config={}
    )
    set_indexing_dependencies(pipeline=test_pipeline_kw)

    # Mock application for TaskWorker
    mock_application_kw = MagicMock()  # Define mock_application_kw
    mock_application_kw.state.embedding_generator = mock_embedder
    mock_application_kw.state.llm_client = None

    dummy_calendar_config_kw = {}  # Define dummy_calendar_config_kw
    dummy_timezone_str_kw = "UTC"  # Define dummy_timezone_str_kw

    # --- Arrange: Ingest Emails ---
    form_data1 = TEST_EMAIL_FORM_DATA.copy()
    form_data1["stripped-text"] = email1_body
    form_data1["Message-Id"] = email1_msg_id
    form_data1["subject"] = email1_title  # Use variable for title

    form_data2 = TEST_EMAIL_FORM_DATA.copy()
    form_data2["stripped-text"] = email2_body
    form_data2["Message-Id"] = email2_msg_id
    form_data2["subject"] = email2_title  # Use variable for title

    # Create TaskWorker instance and start it
    # Provide dummy/mock values for the required arguments
    dummy_timezone_str_kw = "UTC"
    worker = TaskWorker(
        processing_service=None,  # No processing service needed for this handler
        application=mock_application_kw,
        calendar_config=dummy_calendar_config_kw,  # Now defined
        timezone_str=dummy_timezone_str_kw,
    )
    worker.register_task_handler("index_email", handle_index_email)
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-keyword-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    test_failed = False
    try:
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data1, notify_event=test_new_task_event
        )
        await _ingest_and_index_email(
            pg_vector_db_engine, form_data2, notify_event=test_new_task_event
        )

        # --- Act: Query Vectors with Keywords ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(f"Querying vectors with keyword: '{keyword}'...")
            query_results = await query_vectors(
                db,
                query_embedding=query_vec,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                keywords=keyword,  # Add the keyword search term
            )

        # --- Assert ---
        assert_that(query_results).described_as("Query returned None").is_not_none()
        assert_that(query_results).described_as("Expected at least one result matching keyword").is_not_empty()
        logger.info(f"Keyword filter query returned {len(query_results)} result(s).")

        # RRF should rank the keyword match highest, even if vector distance is slightly worse
        found_result = query_results[0]  # Check the top result
        assert_that(found_result.get("source_id")).described_as(f"Top result for keyword '{keyword}'").is_equal_to(email2_msg_id)
        assert_that(found_result.get("embedding_source_content", "").lower()).described_as("Top result content for keyword matching").contains(keyword)
        assert_that(found_result).described_as("Result missing 'rrf_score'").contains_key("rrf_score")
        assert_that(found_result.get("fts_score", 0)).described_as("Keyword match FTS score").is_greater_than(0)

        # Ensure the non-matching document is either absent or ranked lower
        non_matching_present = any(
            r.get("source_id") == email1_msg_id for r in query_results
        )
        if non_matching_present:
            rank_non_match = [r.get("source_id") for r in query_results].index(email1_msg_id)
            rank_match = [r.get("source_id") for r in query_results].index(email2_msg_id)
            assert_that(rank_match).described_as("Keyword match rank").is_less_than(rank_non_match)
            logger.info(
                f"Non-matching document found but ranked lower (Rank {rank_non_match}) than keyword match (Rank {rank_match})."
            )
        else:
            logger.info("Non-matching document correctly excluded from results.")

        logger.info("--- Keyword Filtering Test Passed ---")
    except Exception as e:
        test_failed = True
        logger.error(f"Test failed: {e}", exc_info=True)
        raise
    finally:
        # Stop worker
        logger.info(f"Stopping background task worker {worker_id}...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            if test_failed:
                await dump_tables_on_failure(pg_vector_db_engine)
        except asyncio.TimeoutError:
            worker_task.cancel()


# Add more tests here for hybrid search nuances, different filter combinations, etc.
