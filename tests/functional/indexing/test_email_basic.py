"""
End-to-end functional tests for basic email indexing and vector search.
Tests fundamental email ingestion, indexing, and vector query retrieval.
"""

import asyncio
import logging
import os
import re
import tempfile
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest
import pytest_asyncio
from assertpy import assert_that
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.email_indexer import EmailIndexer
from family_assistant.indexing.pipeline import IndexingPipeline
from family_assistant.indexing.processors.dispatch_processors import (
    EmbeddingDispatchProcessor,
)
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import (
    DocumentEmbeddingRecord,
    DocumentRecord,
    query_vectors,
)
from family_assistant.task_worker import TaskWorker
from family_assistant.web.app_creator import app as fastapi_app
from tests.helpers import wait_for_tasks_to_complete

logger = logging.getLogger(__name__)


def _create_mock_processing_service() -> MagicMock:
    """Create a mock ProcessingService with required attributes."""
    mock = MagicMock()
    return mock


# --- Test Configuration ---
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
    "Date": (datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S %z")),  # RFC 2822 format
    "timestamp": str(int(datetime.now(UTC).timestamp())),
    "token": "dummy_token_e2e",
    "signature": "dummy_signature_e2e",
    "message-headers": (
        f'[["Subject", "{TEST_EMAIL_SUBJECT}"], ["From", "Project Manager <{TEST_EMAIL_SENDER}>"], ["To", "Team Inbox <{TEST_EMAIL_RECIPIENT}>"]]'
    ),  # Simplified headers
}

TEST_QUERY_TEXT = "meeting about Project Alpha"  # Text relevant to the subject/body


# --- Debugging Helper ---
async def dump_tables_on_failure(engine: AsyncEngine) -> None:
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


# --- Fixtures ---
@pytest_asyncio.fixture(scope="function")
async def http_client(
    pg_vector_db_engine: AsyncEngine,  # Ensure DB is setup before app starts
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[httpx.AsyncClient]:
    """
    Provides a test client for the FastAPI application, configured with
    a temporary directory for attachment storage and a mock embedding generator.
    """
    original_app_state_config_present = hasattr(fastapi_app.state, "config")
    original_app_state_config_value = getattr(fastapi_app.state, "config", None)
    original_embedding_generator = getattr(
        fastapi_app.state, "embedding_generator", None
    )

    current_test_config = {}
    if original_app_state_config_present and isinstance(
        original_app_state_config_value, dict
    ):
        current_test_config = original_app_state_config_value.copy()
    elif original_app_state_config_present:
        logger.warning(
            f"app.state.config was present but not a dict ({type(original_app_state_config_value)}). Overwriting."
        )

    # Setup mock embedding generator for the app state
    mock_embedder_for_fixture = MockEmbeddingGenerator(
        embedding_map={},
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )
    fastapi_app.state.embedding_generator = mock_embedder_for_fixture
    logger.info("Test http_client: Set mock_embedding_generator on fastapi_app.state.")

    # Set the database engine in app.state for the get_db dependency
    original_database_engine = getattr(fastapi_app.state, "database_engine", None)
    fastapi_app.state.database_engine = pg_vector_db_engine
    logger.info("Test http_client: Set database_engine in app.state.")

    with tempfile.TemporaryDirectory() as temp_attachment_dir:
        logger.info(
            f"Test http_client: Using temporary attachment directory: {temp_attachment_dir}"
        )
        current_test_config["attachment_storage_path"] = temp_attachment_dir
        current_test_config["mailbox_raw_dir"] = os.path.join(
            temp_attachment_dir, "raw_mailbox_dumps"
        )
        os.makedirs(current_test_config["mailbox_raw_dir"], exist_ok=True)
        fastapi_app.state.config = current_test_config
        logger.info(
            f"Test http_client: Set app.state.config with temp paths: {current_test_config}"
        )

        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
        logger.info("Test HTTP client closed.")

    # Teardown
    if original_app_state_config_present:
        fastapi_app.state.config = original_app_state_config_value
    elif hasattr(fastapi_app.state, "config"):
        delattr(fastapi_app.state, "config")
    logger.info("Test http_client: Restored original app.state.config.")

    if original_embedding_generator is not None:
        fastapi_app.state.embedding_generator = original_embedding_generator
    elif hasattr(fastapi_app.state, "embedding_generator"):
        delattr(fastapi_app.state, "embedding_generator")
    logger.info("Test http_client: Restored original app.state.embedding_generator.")

    # Restore database engine
    if original_database_engine is not None:
        fastapi_app.state.database_engine = original_database_engine
    elif hasattr(fastapi_app.state, "database_engine"):
        delattr(fastapi_app.state, "database_engine")
    logger.info("Test http_client: Restored original app.state.database_engine.")


# --- Helper Function for Test Setup ---


async def _ingest_and_index_email(
    http_client: httpx.AsyncClient,  # Changed to http_client
    engine: AsyncEngine,  # Still needed for DB checks
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    form_data_dict: dict[str, Any],  # Raw form data for the API
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    files_to_upload: dict[str, Any] | None = None,  # For attachments
    task_timeout: float = 15.0,
    notify_event: asyncio.Event | None = None,
) -> int:
    """
    Helper to ingest an email via API, notify worker, wait for indexing, and return Email DB ID.
    """
    email_db_id = None
    indexing_task_id = (
        None  # Keep for logging, though not directly used for waiting on specific task
    )
    message_id = form_data_dict.get("Message-Id", "UNKNOWN_MESSAGE_ID")

    logger.info(
        f"Helper: Calling API to ingest test email with Message-ID: {message_id}"
    )
    # Construct multipart data. Simple key-values go to 'data', files to 'files'.
    # Mailgun sends everything as fields in multipart/form-data.
    # httpx's `data` param handles this for string values.
    # For file uploads, use the `files` param.
    # If TEST_EMAIL_FORM_DATA contains file-like objects or paths, they need to be handled.
    # For now, assuming form_data_dict contains only string data for Mailgun fields.
    # If attachments are added, files_to_upload will be e.g. {"attachment-1": ("filename.pdf", BytesIO(b"..."), "application/pdf")}

    response = await http_client.post(
        "/webhook/mail", data=form_data_dict, files=files_to_upload
    )

    assert_that(response.status_code).described_as(
        f"API call to /webhook/mail failed: {response.status_code} - {response.text}"
    ).is_equal_to(200)
    logger.info(f"Helper: API call for Message-ID {message_id} successful.")

    # After API call, the email should be in DB and task enqueued.
    # Fetch the email ID and task ID from the database
    async with DatabaseContext(engine=engine) as db:
        # Wait briefly for task to likely appear in DB after API commit
        await asyncio.sleep(0.2)
        select_email_stmt = select(
            received_emails_table.c.id, received_emails_table.c.indexing_task_id
        ).where(received_emails_table.c.message_id_header == message_id)
        email_info = await db.fetch_one(select_email_stmt)

        assert_that(email_info).described_as(
            f"Failed to retrieve ingested email {message_id} from DB after API call"
        ).is_not_none()
        email_db_id = email_info["id"]  # type: ignore
        indexing_task_id = email_info["indexing_task_id"]  # type: ignore
        assert_that(email_db_id).described_as(
            f"Email DB ID is null for {message_id} after API call"
        ).is_not_none()
        logger.info(
            f"Helper: Email ingested via API (DB ID: {email_db_id}, Task ID: {indexing_task_id})"
        )

    # Signal the worker if an event is provided
    if notify_event:
        notify_event.set()
        logger.info("Helper: Notified task worker event.")

    # Wait for all tasks to complete
    logger.info(
        f"Helper: Waiting for all pending tasks to complete after ingesting email DB ID {email_db_id} (initial task: {indexing_task_id})..."
    )
    await wait_for_tasks_to_complete(
        engine,
        timeout_seconds=task_timeout,
    )
    logger.info(
        f"Helper: All pending tasks reported as complete for email DB ID {email_db_id}."
    )

    return email_db_id


# --- Test Functions ---


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexing_and_query_e2e(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
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
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
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

    # --- Arrange: Instantiate Email Indexer ---
    email_indexer_instance = EmailIndexer(pipeline=test_pipeline)
    logger.info("Instantiated EmailIndexer for email indexing.")

    # --- Arrange: Register Task Handler ---
    # Create a TaskWorker instance for this test and register the handler
    # Provide dummy/mock values for the required arguments
    # The TaskWorker needs access to the embedding_generator for handle_embed_and_store_batch.
    # This is typically provided via application.state.embedding_generator.
    # The http_client fixture doesn't set this, so we ensure it's set on the global app
    # or pass a mock application to the TaskWorker.
    # For consistency with document_indexer test, let's assume fastapi_app.state can be used
    # if the TaskWorker is instantiated with `application=fastapi_app`.
    # However, to keep test dependencies clear, we'll use a MagicMock for application
    # and set its state, similar to before.
    mock_application_e2e = MagicMock()
    mock_application_e2e.state.embedding_generator = mock_embedder
    mock_application_e2e.state.llm_client = (
        None  # Or a mock LLM if any processor uses it
    )
    # If ATTACHMENT_STORAGE_DIR is needed by any task run by worker via app.state.config:

    dummy_timezone_str = "UTC"  # Not used by email/embedding tasks
    mock_chat_interface_e2e = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event early
    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),  # No processing service needed for this handler
        chat_interface=mock_chat_interface_e2e,
        embedding_generator=mock_embedder,  # Pass the embedder directly
        calendar_config=None,
        timezone_str=dummy_timezone_str,
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker.register_task_handler(
        "index_email", email_indexer_instance.handle_index_email
    )  # Use instance method
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)
    logger.info("TaskWorker created and 'index_email' task handler registered.")

    # --- Act: Start Background Worker ---
    # Start the worker in the background *before* ingesting
    worker_id = f"test-worker-{uuid.uuid4()}"
    test_new_task_event = asyncio.Event()  # Worker will wait on this
    # No need to reassign module-level events since we'll use our own worker instance

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    logger.info(f"Started background task worker {worker_id}...")
    await asyncio.sleep(0.1)  # Give worker time to start
    test_failed = False

    try:
        try:
            # --- Act: Ingest Email via API and Wait for Indexing ---
            # TEST_EMAIL_FORM_DATA is a dict of strings, suitable for `data` param of httpx.post
            # No file attachments in this basic test case yet.
            await _ingest_and_index_email(
                http_client=http_client,
                engine=pg_vector_db_engine,
                form_data_dict=TEST_EMAIL_FORM_DATA,
                files_to_upload=None,  # No attachments for this test
                notify_event=test_new_task_event,
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
            assert_that(query_results).described_as(
                "query_vectors returned None"
            ).is_not_none()
            assert_that(query_results).described_as(
                "No results returned from vector query"
            ).is_not_empty()
            logger.info(f"Query returned {len(query_results)} result(s).")

            # Find the result corresponding to our document
            found_result = None
            for result in query_results:
                if result.get("source_id") == TEST_EMAIL_MESSAGE_ID:
                    found_result = result
                    break

            assert_that(found_result).described_as(
                f"Ingested email (Source ID: {TEST_EMAIL_MESSAGE_ID}) not found in query results: {query_results}"
            ).is_not_none()
            assert found_result is not None  # For type checker
            logger.info(f"Found matching result: {found_result}")

            # Check distance (should be small since query embedding was close to body)
            assert_that(found_result).described_as(
                f"Result missing 'distance' field: {found_result}"
            ).contains_key("distance")
            assert_that(found_result["distance"]).described_as(
                "Distance should be small"
            ).is_less_than(0.1)

            # Check other fields in the result
            assert_that(found_result.get("embedding_type")).is_in(
                "raw_body_text_chunk", "title_chunk"
            )
            if found_result.get("embedding_type") == "raw_body_text_chunk":
                assert_that(found_result.get("embedding_source_content")).is_equal_to(
                    TEST_EMAIL_BODY
                )
            else:
                assert_that(found_result.get("embedding_source_content")).is_equal_to(
                    TEST_EMAIL_SUBJECT
                )

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

        except TimeoutError:
            logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
            worker_task.cancel()
        except Exception as e:
            logger.error(f"Error stopping worker task {worker_id}: {e}", exc_info=True)
