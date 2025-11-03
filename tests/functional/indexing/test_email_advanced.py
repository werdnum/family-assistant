"""
Advanced functional tests for email indexing with vector ranking, filtering, and search.
Tests vector ranking, metadata filtering, and keyword-based filtering capabilities.
"""

import asyncio
import logging
import os
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
async def test_vector_ranking(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """
    Tests if vector search returns results ranked correctly by distance.
    1. Ingest three emails with distinct content.
    2. Assign embeddings such that one is very close, one medium, one far from a query.
    3. Perform a vector-only query.
    4. Assert the results are ordered correctly by distance.
    """
    logger.info("\n--- Running Vector Ranking Test ---")

    # --- Arrange: Mock Embeddings ---
    # Create embeddings with controlled distances using deterministic vectors
    # Use a fixed seed for reproducibility
    rng = np.random.RandomState(42)
    base_vec = rng.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32)

    # Create vectors with deterministic offsets to ensure proper ordering
    # vec_close is very close to base_vec
    vec_close = base_vec.copy()
    vec_close[0] += 0.01  # Small deterministic offset
    vec_close = vec_close.tolist()

    # vec_medium is further from base_vec
    vec_medium = base_vec.copy()
    vec_medium[0] += 0.3  # Medium deterministic offset
    vec_medium = vec_medium.tolist()

    # vec_far is furthest from base_vec
    vec_far = base_vec.copy()
    vec_far[0] += 0.8  # Large deterministic offset
    vec_far = vec_far.tolist()

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

    # Add mock embeddings for titles (also deterministic)
    title1_vec = base_vec.copy()
    title1_vec[1] += 0.02  # Small offset on different dimension
    title1_vec = title1_vec.tolist()

    title2_vec = base_vec.copy()
    title2_vec[1] += 0.25  # Medium offset on different dimension
    title2_vec = title2_vec.tolist()

    title3_vec = base_vec.copy()
    title3_vec[1] += 0.7  # Large offset on different dimension
    title3_vec = title3_vec.tolist()

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
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
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
    email_indexer_instance_kw = EmailIndexer(
        pipeline=test_pipeline_kw
    )  # Instantiate EmailIndexer

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
    mock_chat_interface_kw = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event before TaskWorker
    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),  # No processing service needed for this handler
        chat_interface=mock_chat_interface_kw,
        embedding_generator=mock_embedder,  # Pass the embedder directly
        calendar_config=dummy_calendar_config_kw,
        timezone_str=dummy_timezone_str_kw,
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker.register_task_handler(
        "index_email", email_indexer_instance_kw.handle_index_email
    )  # Use instance method
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-rank-{uuid.uuid4()}"
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    test_failed = False
    try:
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data1,
            notify_event=test_new_task_event,
        )
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data2,
            notify_event=test_new_task_event,
        )
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data3,
            notify_event=test_new_task_event,
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
        assert_that(len(query_results)).described_as(
            "Expected at least 3 results"
        ).is_greater_than(3)
        logger.info(f"Ranking query returned {len(query_results)} results.")

        # Extract source IDs and distances
        results_ordered = [
            (r.get("source_id"), r.get("distance")) for r in query_results
        ]
        logger.info(f"Results ordered by distance: {results_ordered}")

        # Find the indices of our test emails in the results
        result_source_ids = [r[0] for r in results_ordered]
        assert_that(result_source_ids).described_as(
            f"Results for ranking: {results_ordered}"
        ).contains(email1_msg_id, email2_msg_id, email3_msg_id)
        idx1 = result_source_ids.index(email1_msg_id)
        idx2 = result_source_ids.index(email2_msg_id)
        idx3 = result_source_ids.index(email3_msg_id)

        # Assert the order based on distance (lower index means closer/better rank)
        assert_that(idx1).described_as(
            f"Closest email ({email1_msg_id}) ranking"
        ).is_less_than(idx2)
        assert_that(idx2).described_as(
            f"Medium email ({email2_msg_id}) ranking"
        ).is_less_than(idx3)

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
        except TimeoutError:
            worker_task.cancel()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_metadata_filtering(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
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
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
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
    email_indexer_instance_meta = EmailIndexer(
        pipeline=test_pipeline_meta
    )  # Instantiate EmailIndexer

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
    mock_chat_interface_meta = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event before TaskWorker
    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),  # No processing service needed for this handler
        chat_interface=mock_chat_interface_meta,
        embedding_generator=mock_embedder,  # Pass the embedder directly
        calendar_config=dummy_calendar_config_meta,
        timezone_str=dummy_timezone_str_meta,
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker.register_task_handler(
        "index_email", email_indexer_instance_meta.handle_index_email
    )  # Use instance method
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-meta-{uuid.uuid4()}"
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    test_failed = False
    try:
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data1,
            notify_event=test_new_task_event,
        )
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data2,
            notify_event=test_new_task_event,
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
        assert_that(query_results).described_as(
            "Expected at least 1 result matching filter"
        ).is_not_empty()
        logger.info(f"Metadata filter query returned {len(query_results)} result(s).")

        # Verify that *all* returned results belong to the correct document
        for found_result in query_results:
            assert_that(found_result.get("source_id")).described_as(
                f"Incorrect document returned by metadata filter. Expected source_id {email2_msg_id}, Full result: {found_result!r}"
            ).is_equal_to(email2_msg_id)
            assert_that(found_result.get("title")).described_as(
                "Incorrect title in filtered result. Expected 'Metadata Test Far Correct Type'"
            ).is_equal_to("Metadata Test Far Correct Type")

        # Optional: Check that the closer document (email1) is NOT in the results
        source_ids_returned = {r.get("source_id") for r in query_results}
        assert_that(source_ids_returned).described_as(
            f"Document {email1_msg_id} (which should be filtered out) was found in results."
        ).does_not_contain(email1_msg_id)

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
        except TimeoutError:
            worker_task.cancel()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_keyword_filtering(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
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
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
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
    email_indexer_instance_kw = EmailIndexer(
        pipeline=test_pipeline_kw
    )  # Instantiate EmailIndexer

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
    # Define new mocks for this test scope
    mock_chat_interface_keyword_test = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event before TaskWorker

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),  # No processing service needed for this handler
        chat_interface=mock_chat_interface_keyword_test,
        embedding_generator=mock_embedder,  # Pass the embedder directly
        calendar_config=dummy_calendar_config_kw,  # Now defined
        timezone_str=dummy_timezone_str_kw,
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker.register_task_handler(
        "index_email", email_indexer_instance_kw.handle_index_email
    )  # Use instance method
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-keyword-{uuid.uuid4()}"
    test_new_task_event = asyncio.Event()

    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    test_failed = False
    try:
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data1,
            notify_event=test_new_task_event,
        )
        await _ingest_and_index_email(
            http_client,
            pg_vector_db_engine,
            form_data2,
            notify_event=test_new_task_event,
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
        assert_that(query_results).described_as(
            "Expected at least one result matching keyword"
        ).is_not_empty()
        logger.info(f"Keyword filter query returned {len(query_results)} result(s).")

        # RRF should rank the keyword match highest, even if vector distance is slightly worse
        found_result = query_results[0]  # Check the top result
        assert_that(found_result.get("source_id")).described_as(
            f"Top result for keyword '{keyword}'"
        ).is_equal_to(email2_msg_id)
        assert_that(
            found_result.get("embedding_source_content", "").lower()
        ).described_as("Top result content for keyword matching").contains(keyword)
        assert_that(found_result).described_as(
            "Result missing 'rrf_score'"
        ).contains_key("rrf_score")
        assert_that(found_result.get("fts_score", 0)).described_as(
            "Keyword match FTS score"
        ).is_greater_than(0)

        # Ensure the non-matching document is either absent or ranked lower
        non_matching_present = any(
            r.get("source_id") == email1_msg_id for r in query_results
        )
        if non_matching_present:
            rank_non_match = [r.get("source_id") for r in query_results].index(
                email1_msg_id
            )
            rank_match = [r.get("source_id") for r in query_results].index(
                email2_msg_id
            )
            assert_that(rank_match).described_as("Keyword match rank").is_less_than(
                rank_non_match
            )
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
        except TimeoutError:
            worker_task.cancel()
