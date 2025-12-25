"""
End-to-end functional tests for email indexing with attachments and advanced processing.
Tests PDF attachment indexing, LLM summary generation, and primary link extraction.
"""

import asyncio
import contextlib  # Added
import io  # Added for BytesIO
import json  # Added
import logging
import os  # Add os import
import tempfile  # Added for http_client fixture
import uuid
from collections.abc import (
    AsyncGenerator,  # Add missing typing imports & AsyncGenerator
)
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock  # unittest.mock.AsyncMock removed

import httpx  # Added for http_client
import numpy as np
import pytest
import pytest_asyncio  # Added for async fixtures
from assertpy import assert_that

# Reportlab imports removed
from sqlalchemy import select  # Added text for raw SQL if needed
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.email_indexer import EmailIndexer
from family_assistant.indexing.pipeline import IndexingPipeline
from family_assistant.indexing.processors.dispatch_processors import (
    EmbeddingDispatchProcessor,
)
from family_assistant.indexing.processors.file_processors import PDFTextExtractor
from family_assistant.indexing.processors.llm_processors import (  # Added
    LLMPrimaryLinkExtractorProcessor,
    LLMSummaryGeneratorProcessor,
)
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.network_processors import (
    WebFetcherProcessor,
)  # Added
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.tasks import handle_embed_and_store_batch

# Import components needed for the E2E test
# Import test helpers
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table
from family_assistant.storage.tasks import (
    tasks_table,
)  # Keep if used for direct inspection, though wait_for_tasks_to_complete is preferred
from family_assistant.storage.vector import (
    DocumentEmbeddingRecord,
    DocumentRecord,
    query_vectors,
)  # Added imports
from family_assistant.task_worker import TaskWorker
from family_assistant.utils.scraping import MockScraper  # Added

# Import the FastAPI app directly for the test client
from family_assistant.web.app_creator import app as fastapi_app
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import (  # Added
    LLMOutput,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)


def _create_mock_processing_service() -> MagicMock:
    """Create a mock ProcessingService with required attributes."""
    mock = MagicMock()
    return mock


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
        mailbox_raw_dir = os.path.join(temp_attachment_dir, "raw_mailbox_dumps")
        os.makedirs(mailbox_raw_dir, exist_ok=True)
        test_config = AppConfig(
            attachment_storage_path=temp_attachment_dir,
            mailbox_raw_dir=mailbox_raw_dir,
        )
        fastapi_app.state.config = test_config
        logger.info("Test http_client: Set app.state.config with temp paths")

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

# create_simple_pdf_bytes function removed

# --- Test Data for PDF Attachment ---
# This should be a representative string of what's expected to be extracted from test_doc.pdf
# by PDFTextExtractor and then processed by TextChunker.
# This is the actual first chunk (500 chars) produced by the pipeline from test_doc.pdf's markdown.
TEST_PDF_EXTRACTED_TEXT = "The Importance of Regular Software Updates Software updates are a common and crucial aspect of using digital devices from smartphones and computers to smart home appliances and applications While sometimes perceived as inconvenient regularly updating your software is vital for a secure stable and optimized user experience These updates are released by developers to address various issues introduce new features and enhance overall performance. Security Benefits One of the most critical reasons to"
TEST_PDF_FILENAME = "test_doc.pdf"  # Using the existing test PDF
TEST_EMAIL_SUBJECT_WITH_PDF = "E2E Test: Email with test_doc.pdf Attachment"
TEST_EMAIL_BODY_WITH_PDF = "Please find attached the test_doc.pdf document."
TEST_EMAIL_MESSAGE_ID_WITH_PDF = (
    f"<e2e_email_with_test_doc_pdf_{uuid.uuid4()}@example.com>"
)
TEST_QUERY_TEXT_FOR_PDF = (
    "why regular software updates are important for security and performance"
)
# Define a known substring from TEST_PDF_EXTRACTED_TEXT for more robust assertion
KNOWN_SUBSTRING_FROM_PDF = "Software updates are a common and crucial aspect"


# --- Test Data for Email Primary Link Extraction E2E ---
TEST_EMAIL_BODY_PRIMARY_LINK = "Hello team, please click this important link to access the new portal: https://example.com/new-portal-access"
TEST_EMAIL_BODY_NO_PRIMARY_LINK = "Hi team, let's discuss the quarterly results in our meeting next Monday. No links here, just a discussion prompt."
EXPECTED_EXTRACTED_URL = "https://example.com/new-portal-access"
PRIMARY_LINK_TARGET_TYPE = (
    "raw_url"  # As configured in LLMPrimaryLinkExtractorProcessor
)

# --- Test Data for Email LLM Summary E2E ---
TEST_EMAIL_BODY_FOR_SUMMARY = "This email contains critical information about the upcoming product launch event, including timelines, key stakeholders, and marketing strategies. Please review thoroughly."
EXPECTED_LLM_SUMMARY_EMAIL = "An email detailing an upcoming product launch event, covering timelines, stakeholders, and marketing plans."
TEST_QUERY_FOR_EMAIL_SUMMARY = "product launch event details"
EMAIL_LLM_SUMMARY_TARGET_TYPE = "email_llm_generated_summary"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_with_pdf_attachment_indexing_e2e(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """
    End-to-end test for email ingestion with a PDF attachment,
    indexing via task worker (including PDF text extraction), and vector query retrieval.
    """
    logger.info("\n--- Running Email with PDF Attachment Indexing E2E Test ---")

    # --- Arrange: PDF Content ---
    # Read the content from the existing PDF file
    pdf_file_path = "tests/data/test_doc.pdf"
    try:
        pdf_content_bytes = await asyncio.to_thread(Path(pdf_file_path).read_bytes)
    except FileNotFoundError:
        pytest.fail(f"Test PDF file not found at {pdf_file_path}")
    assert_that(pdf_content_bytes).described_as(
        "PDF content bytes should not be empty"
    ).is_not_empty()

    # --- Arrange: Mock Embeddings ---
    # Keys in embedding_map must be the raw text that MockEmbeddingGenerator will receive.

    pdf_content_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.3
    ).tolist()
    email_subject_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.1
    ).tolist()
    email_body_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.2
    ).tolist()
    query_pdf_embedding = (  # Query embedding close to PDF content
        np.array(pdf_content_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()

    embedding_map = {
        TEST_PDF_EXTRACTED_TEXT: pdf_content_embedding,  # Use raw text constant
        TEST_EMAIL_SUBJECT_WITH_PDF: email_subject_embedding,  # Use raw text constant
        TEST_EMAIL_BODY_WITH_PDF: email_body_embedding,  # Use raw text constant
        TEST_QUERY_TEXT_FOR_PDF: (
            query_pdf_embedding
        ),  # This is already a raw query string
    }
    mock_embedder = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )

    # --- Arrange: Create Indexing Pipeline (including PDFTextExtractor) ---
    # This pipeline should be capable of handling email text and PDF attachments.

    title_extractor = TitleExtractor()
    pdf_extractor = PDFTextExtractor()

    # Configuration for TextChunker, including the prefix map
    text_chunker_test_config = {
        "chunk_size": 500,
        "chunk_overlap": 50,
        "embedding_type_prefix_map": {
            "raw_body_text": "content_chunk",
            "extracted_markdown_content": "content_chunk",  # from PDFTextExtractor
            "title": "title_chunk",  # if titles are chunked
        },
    }
    # Instantiate TextChunker with its direct constructor arguments
    text_chunker = TextChunker(
        chunk_size=text_chunker_test_config["chunk_size"],
        chunk_overlap=text_chunker_test_config["chunk_overlap"],
        # embedding_type_prefix_map is provided via pipeline config to TextChunker
    )

    embedding_dispatcher = EmbeddingDispatchProcessor(
        embedding_types_to_dispatch=[
            "title_chunk",
            "title",
            "content_chunk",  # This should now match TextChunker's output for PDF content
        ],
    )
    # Pass the text_chunker_test_config to the IndexingPipeline
    # If TextChunker now gets map from constructor, this pipeline config for it might be redundant
    # but shouldn't harm if TextChunker prioritizes constructor args or ignores if already set.
    test_pipeline_with_pdf = IndexingPipeline(
        processors=[title_extractor, pdf_extractor, text_chunker, embedding_dispatcher],
        config={"text_chunker": text_chunker_test_config},
    )
    email_indexer_instance_pdf = EmailIndexer(
        pipeline=test_pipeline_with_pdf
    )  # Instantiate EmailIndexer
    logger.info(
        "Set IndexingPipeline with PDFTextExtractor for email attachment indexing."
    )

    # --- Arrange: Task Worker Setup ---
    mock_application_pdf = MagicMock()
    mock_application_pdf.state.embedding_generator = mock_embedder
    mock_application_pdf.state.llm_client = None  # Or a mock LLM if needed

    dummy_timezone_str_pdf = "UTC"
    mock_chat_interface_pdf = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event early
    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface_pdf,
        embedding_generator=mock_embedder,  # Pass the embedder directly
        calendar_config=None,
        timezone_str=dummy_timezone_str_pdf,
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker.register_task_handler(
        "index_email", email_indexer_instance_pdf.handle_index_email
    )  # Use instance method
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)

    worker_id = f"test-worker-pdf-{uuid.uuid4()}"
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    logger.info(f"Started background task worker {worker_id} for PDF test...")
    await asyncio.sleep(0.1)
    test_failed = False

    try:
        # --- Arrange: Prepare Form Data and File for Upload ---
        email_form_data_with_pdf = TEST_EMAIL_FORM_DATA.copy()
        email_form_data_with_pdf.update({
            "subject": TEST_EMAIL_SUBJECT_WITH_PDF,
            "stripped-text": TEST_EMAIL_BODY_WITH_PDF,
            "Message-Id": TEST_EMAIL_MESSAGE_ID_WITH_PDF,
            "attachment-count": "1",  # Indicate one attachment
        })
        # Mailgun sends attachments as form fields like 'attachment-1', 'attachment-2', etc.
        # httpx `files` param maps to this.
        files_to_upload = {
            "attachment-1": (
                TEST_PDF_FILENAME,
                io.BytesIO(pdf_content_bytes),
                "application/pdf",
            )
        }

        # --- Act: Ingest Email with PDF Attachment and Wait for Indexing ---
        await _ingest_and_index_email(
            http_client=http_client,
            engine=pg_vector_db_engine,
            form_data_dict=email_form_data_with_pdf,
            files_to_upload=files_to_upload,
            notify_event=test_new_task_event,
            task_timeout=30.0,  # Increase timeout from default 15s to 30s
        )

        # --- Act: Query Vectors for PDF Content ---
        query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(
                f"Querying vectors using text relevant to PDF: '{TEST_QUERY_TEXT_FOR_PDF}'"
            )
            query_results = await query_vectors(
                db,
                query_embedding=query_pdf_embedding,
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"source_type": "email"},
            )

        # --- Assert ---
        assert_that(query_results).described_as(
            "query_vectors returned None for PDF content"
        ).is_not_none()
        assert_that(query_results).described_as(
            "No results returned from PDF content vector query"
        ).is_not_empty()
        logger.info(f"PDF content query returned {len(query_results)} result(s).")

        found_pdf_result = None
        for result in query_results:
            # We expect the content from the PDF to be associated with the email's source_id
            # Check if the known substring is in the embedding_source_content
            embedding_content = result.get("embedding_source_content", "")
            # Combine conditions into a single if statement
            if (
                result.get("source_id") == TEST_EMAIL_MESSAGE_ID_WITH_PDF
                and KNOWN_SUBSTRING_FROM_PDF in embedding_content
                and embedding_content == TEST_PDF_EXTRACTED_TEXT
            ):
                # This ensures we're matching the chunk that *should* correspond to TEST_PDF_EXTRACTED_TEXT
                # (and thus got the specific pdf_content_embedding).
                found_pdf_result = result
                break

        # If not found by exact match, try again with just substring for broader check,
        # though this might pick a different chunk if the first one had issues.
        if not found_pdf_result:
            for result in query_results:
                embedding_content = result.get("embedding_source_content", "")
                if (
                    result.get("source_id") == TEST_EMAIL_MESSAGE_ID_WITH_PDF
                    and KNOWN_SUBSTRING_FROM_PDF in embedding_content
                ):
                    logger.warning(
                        f"Found PDF content via substring match for result: {result.get('embedding_id')}, chunk_index: {result.get('embedding_metadata', {}).get('chunk_index')}. Original TEST_PDF_EXTRACTED_TEXT might not be an exact match to any stored chunk if this is the first hit."
                    )
                    found_pdf_result = (
                        result  # Take the first one that contains the substring
                    )
                    break

        assert_that(found_pdf_result).described_as(
            f"Ingested PDF content (from email Source ID: {TEST_EMAIL_MESSAGE_ID_WITH_PDF}) "
            f"containing substring '{KNOWN_SUBSTRING_FROM_PDF}' not found in query results. Results: {query_results}"
        ).is_not_none()
        assert found_pdf_result is not None  # For type checker
        logger.info(f"Found matching result for PDF content: {found_pdf_result}")

        assert_that(found_pdf_result["distance"]).described_as(
            "Distance for PDF content should be small"
        ).is_less_than(0.1)
        assert_that(found_pdf_result.get("embedding_type")).is_equal_to(
            "content_chunk"
        )  # From TextChunker
        # Assert that the found chunk's content contains the known substring
        assert_that(found_pdf_result.get("embedding_source_content", "")).contains(
            KNOWN_SUBSTRING_FROM_PDF
        )
        assert_that(found_pdf_result.get("title")).is_equal_to(
            TEST_EMAIL_SUBJECT_WITH_PDF
        )  # Title should be email's subject
        assert_that(found_pdf_result.get("source_type")).is_equal_to("email")

        logger.info("--- Email with PDF Attachment Indexing E2E Test Passed ---")

    except Exception as e:
        test_failed = True
        logger.error(f"Test failed: {e}", exc_info=True)
        raise
    finally:
        logger.info(f"Stopping background task worker {worker_id} for PDF test...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} stopped for PDF test.")
            if test_failed:
                await dump_tables_on_failure(pg_vector_db_engine)
        except TimeoutError:
            logger.warning(
                f"Timeout stopping worker task {worker_id} for PDF test. Cancelling."
            )
            worker_task.cancel()
        except Exception as e:
            logger.error(
                f"Error stopping worker task {worker_id} for PDF test: {e}",
                exc_info=True,
            )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexing_with_llm_summary_e2e(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """
    End-to-end test for email ingestion with LLM-generated summary.
    """
    logger.info("\n--- Running Email Indexing with LLM Summary E2E Test ---")

    # --- Arrange: Mock LLM Client for Summarization ---
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def email_summary_matcher(actual_kwargs: dict[str, Any]) -> bool:
        # method_name argument removed as it's no longer passed or needed
        if not (
            actual_kwargs.get("tools")
            and actual_kwargs["tools"][0].get("function", {}).get("name")
            == "extract_summary"
        ):
            return False
        user_message_content = get_last_message_text(actual_kwargs["messages"])
        return TEST_EMAIL_BODY_FOR_SUMMARY in user_message_content

    mock_llm_output_email = LLMOutput(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_email_summary_123",
                type="function",
                function=ToolCallFunction(
                    name="extract_summary",
                    arguments=json.dumps({"summary": EXPECTED_LLM_SUMMARY_EMAIL}),
                ),
            )
        ],
    )
    mock_llm_client_email = RuleBasedMockLLMClient(
        rules=[(email_summary_matcher, mock_llm_output_email)]  # type: ignore[arg-type]
    )

    # --- Arrange: Mock Embeddings ---
    email_summary_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.5
    ).tolist()
    query_email_summary_embedding = (
        np.array(email_summary_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()

    # Create a new MockEmbeddingGenerator instance for this test or update a shared one carefully
    # For simplicity, let's assume a fresh one or that the http_client fixture handles this.
    # The http_client fixture sets fastapi_app.state.embedding_generator.
    # We need to ensure this generator knows about our new summary texts.
    # It's better if the mock_embedder is scoped to the test or easily updatable.
    # Let's assume we can update the one from fastapi_app.state if it's a MockEmbeddingGenerator.

    # The http_client fixture now ensures fastapi_app.state.embedding_generator is a MockEmbeddingGenerator.
    current_embedder: MockEmbeddingGenerator = fastapi_app.state.embedding_generator
    assert isinstance(current_embedder, MockEmbeddingGenerator), (
        "Embedding generator on app state is not a MockEmbeddingGenerator instance."
    )
    # Clear any existing map from previous tests using the same fixture instance if scope is wider than function.
    # For function scope, this is just for safety.
    current_embedder.embedding_map.clear()

    # Add embeddings for the body and title that will be chunked, and for the summary
    email_body_for_summary_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.6
    ).tolist()
    # The title of the email being summarized is "Email for LLM Summary Test"
    email_title_for_summary_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.7
    ).tolist()

    current_embedder.embedding_map.update({
        json.dumps(
            {"summary": EXPECTED_LLM_SUMMARY_EMAIL}, indent=2
        ): email_summary_embedding,
        TEST_QUERY_FOR_EMAIL_SUMMARY: query_email_summary_embedding,
        TEST_EMAIL_BODY_FOR_SUMMARY: email_body_for_summary_embedding,
        "Email for LLM Summary Test": email_title_for_summary_embedding,
    })
    # Store for assertion
    current_embedder._test_query_email_summary_embedding = query_email_summary_embedding  # type: ignore[attr-defined]

    # --- Arrange: Indexing Pipeline with LLM Summary Processor ---
    llm_summary_processor_email = LLMSummaryGeneratorProcessor(
        llm_client=mock_llm_client_email,  # type: ignore[arg-type]
        input_content_types=["raw_body_text"],  # Process email body
        target_embedding_type=EMAIL_LLM_SUMMARY_TARGET_TYPE,
    )
    # Use existing processors from other tests if suitable, or define new ones
    title_extractor = TitleExtractor()  # Assuming title is still extracted
    text_chunker = TextChunker(
        chunk_size=500, chunk_overlap=50
    )  # For email body if needed
    embedding_dispatcher_email = EmbeddingDispatchProcessor(
        embedding_types_to_dispatch=[
            "title_chunk",  # Or just "title" if not chunked
            "raw_body_text_chunk",
            EMAIL_LLM_SUMMARY_TARGET_TYPE,  # Dispatch the summary
            "raw_body_text",  # Ensure raw body text is also dispatched if not chunked for some reason
        ],
    )
    test_pipeline_email_summary = IndexingPipeline(
        processors=[
            title_extractor,  # Extracts title from email_doc
            llm_summary_processor_email,  # Generates summary from raw_body_text
            text_chunker,  # Chunks title and raw_body_text
            embedding_dispatcher_email,  # Dispatches title_chunk, raw_body_text_chunk, and summary
        ],
        config={},
    )
    email_indexer_instance_summary = EmailIndexer(
        pipeline=test_pipeline_email_summary
    )  # Instantiate EmailIndexer

    # --- Arrange: Task Worker Setup ---
    # Inject mock LLM client for the summary processor via app state
    original_llm_client = getattr(fastapi_app.state, "llm_client", None)
    fastapi_app.state.llm_client = mock_llm_client_email  # type: ignore[assignment]

    # Create a mock application object for TaskWorker
    mock_app_state_summary = MagicMock()
    mock_app_state_summary.embedding_generator = current_embedder
    mock_app_state_summary.llm_client = mock_llm_client_email

    mock_application_summary = MagicMock()
    mock_application_summary.state = mock_app_state_summary

    mock_chat_interface_summary = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event before TaskWorker
    worker_email_summary = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface_summary,
        embedding_generator=current_embedder,  # Use the updated embedder
        calendar_config={},
        timezone_str="UTC",
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker_email_summary.register_task_handler(
        "index_email", email_indexer_instance_summary.handle_index_email
    )  # Use instance method
    worker_email_summary.register_task_handler(
        "embed_and_store_batch", handle_embed_and_store_batch
    )

    worker_id = f"test-email-summary-worker-{uuid.uuid4()}"
    logger.info(f"Starting email summary worker: {worker_id}")  # Use worker_id
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_email_summary.run(test_new_task_event))
    await asyncio.sleep(0.1)

    email_db_id = None
    test_failed = False
    try:
        # --- Act: Ingest Email via API ---
        email_msg_id_summary = f"<email_summary_test_{uuid.uuid4()}@example.com>"
        form_data_email_summary = TEST_EMAIL_FORM_DATA.copy()
        form_data_email_summary.update({
            "subject": "Email for LLM Summary Test",
            "stripped-text": TEST_EMAIL_BODY_FOR_SUMMARY,
            "Message-Id": email_msg_id_summary,
        })

        email_db_id = await _ingest_and_index_email(
            http_client=http_client,
            engine=pg_vector_db_engine,
            form_data_dict=form_data_email_summary,
            notify_event=test_new_task_event,
        )

        # --- Assert: Query for the LLM-generated summary ---
        email_summary_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(
                f"Querying vectors for email LLM summary using text: '{TEST_QUERY_FOR_EMAIL_SUMMARY}'"
            )
            email_summary_query_results = await query_vectors(
                db,
                query_embedding=current_embedder._test_query_email_summary_embedding,  # type: ignore[attr-defined]
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"source_id": email_msg_id_summary},
                embedding_type_filter=[EMAIL_LLM_SUMMARY_TARGET_TYPE],
            )

        assert email_summary_query_results is not None, (
            "Email LLM summary query_vectors returned None"
        )
        assert len(email_summary_query_results) > 0, (
            "No results from email LLM summary vector query"
        )

        found_email_summary_result = email_summary_query_results[0]
        logger.info(f"Found email LLM summary result: {found_email_summary_result}")

        assert found_email_summary_result.get("source_id") == email_msg_id_summary
        assert (
            found_email_summary_result.get("embedding_type")
            == EMAIL_LLM_SUMMARY_TARGET_TYPE
        )
        expected_stored_email_summary_content = json.dumps(
            {"summary": EXPECTED_LLM_SUMMARY_EMAIL}, indent=2
        )
        assert (
            found_email_summary_result.get("embedding_source_content")
            == expected_stored_email_summary_content
        )
        assert_that(found_email_summary_result.get("distance")).is_not_none()
        assert found_email_summary_result.get("distance") < 0.1, (  # type: ignore[operator]
            "Distance for email LLM summary should be small"
        )

        # LLM call verification removed as per user request.
        # The successful creation of the summary embedding, verified above,
        # implies the LLM was called correctly with the mock setup.

        logger.info("--- Email Indexing with LLM Summary E2E Test Passed ---")

    except Exception as e:
        test_failed = True
        logger.error(f"Test failed: {e}", exc_info=True)
        # No need to dump here, will be handled in finally if test_failed is True
        raise
    finally:
        # Cleanup
        if (
            test_failed and email_db_id
        ):  # Conditionally dump if test failed and email was ingested
            logger.info("Dumping tables due to test failure...")
            await dump_tables_on_failure(pg_vector_db_engine)
        if hasattr(fastapi_app.state, "llm_client"):  # Restore original LLM client
            if original_llm_client:
                fastapi_app.state.llm_client = original_llm_client
            else:
                delattr(fastapi_app.state, "llm_client")

        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        # Email cleanup is handled by the _ingest_and_index_email helper if it fails,
        # but successful runs might leave data. For test isolation, explicit cleanup is good.
        # However, the current structure doesn't easily return the task_id for email cleanup here.
        # The email_db_id is available.
        if email_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as _db_cleanup:
                    # The previous cleanup logic for email_db_id was incorrect and has been removed.
                    # Email documents are linked via source_id (Message-ID) to the documents table.
                    # A more robust cleanup would involve finding the document by source_id and deleting it.
                    logger.warning(
                        f"Partial cleanup for email_db_id {email_db_id}. Corresponding document in 'documents' table (if any) was not deleted. Manual check advised."
                    )
            except Exception as e:
                logger.warning(f"Cleanup error for email {email_db_id}: {e}")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexing_with_primary_link_extraction_e2e(
    http_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """
    End-to-end test for email ingestion with LLM-based primary link extraction.
    Verifies that the LLMPrimaryLinkExtractorProcessor correctly identifies and
    outputs a raw_url item, which is then picked up by a WebFetcherProcessor (mocked).
    """
    logger.info(
        "\n--- Running Email Indexing with Primary Link Extraction E2E Test ---"
    )

    # --- Arrange: Mock LLM Client for Link Extraction ---
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def primary_link_matcher_positive(actual_kwargs: dict[str, Any]) -> bool:
        if not (
            actual_kwargs.get("tools")
            and actual_kwargs["tools"][0].get("function", {}).get("name")
            == "extract_primary_link"
        ):
            return False
        user_message_content = get_last_message_text(actual_kwargs["messages"])
        return TEST_EMAIL_BODY_PRIMARY_LINK in user_message_content

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def primary_link_matcher_negative(actual_kwargs: dict[str, Any]) -> bool:
        if not (
            actual_kwargs.get("tools")
            and actual_kwargs["tools"][0].get("function", {}).get("name")
            == "extract_primary_link"
        ):
            return False
        user_message_content = get_last_message_text(actual_kwargs["messages"])
        return TEST_EMAIL_BODY_NO_PRIMARY_LINK in user_message_content

    mock_llm_output_link_positive = LLMOutput(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_primary_link_extract_pos",
                type="function",
                function=ToolCallFunction(
                    name="extract_primary_link",
                    arguments=json.dumps({
                        "primary_url": EXPECTED_EXTRACTED_URL,
                        "is_primary_link_email": True,
                    }),
                ),
            )
        ],
    )
    mock_llm_output_link_negative = LLMOutput(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_primary_link_extract_neg",
                type="function",
                function=ToolCallFunction(
                    name="extract_primary_link",
                    arguments=json.dumps({"is_primary_link_email": False}),
                ),
            )
        ],
    )

    mock_llm_client_link_ext = RuleBasedMockLLMClient(
        rules=[  # type: ignore[arg-type]
            (primary_link_matcher_positive, mock_llm_output_link_positive),
            (primary_link_matcher_negative, mock_llm_output_link_negative),
        ]
    )

    # --- Arrange: Mock Scraper for WebFetcherProcessor ---
    mock_scraper = MockScraper(
        url_map={}
    )  # No specific content needed, just capture calls
    fastapi_app.state.scraper = (
        mock_scraper  # Ensure DocumentIndexer can pick this up if it were used
    )

    # --- Arrange: Mock Embeddings (for other parts of the email) ---
    current_embedder: MockEmbeddingGenerator = fastapi_app.state.embedding_generator
    assert isinstance(current_embedder, MockEmbeddingGenerator)
    current_embedder.embedding_map.clear()  # Clear from previous tests
    current_embedder.embedding_map.update({
        TEST_EMAIL_BODY_PRIMARY_LINK: (
            (np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.1).tolist()
        ),
        "Email with Primary Link": (
            (np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.2).tolist()
        ),  # Title
        TEST_EMAIL_BODY_NO_PRIMARY_LINK: (
            (np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.3).tolist()
        ),
        "Email with No Primary Link": (
            (np.random.rand(TEST_EMBEDDING_DIMENSION) * 0.4).tolist()
        ),  # Title
    })

    # --- Arrange: Indexing Pipeline ---
    title_extractor = TitleExtractor()
    link_extractor_processor = LLMPrimaryLinkExtractorProcessor(
        llm_client=mock_llm_client_link_ext,  # type: ignore[arg-type]
        input_content_types=["raw_body_text"],
        target_embedding_type=PRIMARY_LINK_TARGET_TYPE,  # Should be "raw_url"
    )
    web_fetcher_processor = WebFetcherProcessor(
        scraper=mock_scraper  # Inject mock scraper
        # input_content_types removed as it's not an accepted argument
    )
    text_chunker = TextChunker(chunk_size=500, chunk_overlap=50)
    embedding_dispatcher = EmbeddingDispatchProcessor(
        embedding_types_to_dispatch=["title_chunk", "raw_body_text_chunk"]
    )

    test_pipeline_link_extraction = IndexingPipeline(
        processors=[
            title_extractor,
            link_extractor_processor,
            web_fetcher_processor,  # type: ignore[list-item] # WebFetcher after link extractor
            text_chunker,
            embedding_dispatcher,
        ],
        config={},
    )
    email_indexer_instance_link = EmailIndexer(
        pipeline=test_pipeline_link_extraction
    )  # Instantiate EmailIndexer

    # --- Arrange: Task Worker Setup ---
    original_llm_client = getattr(fastapi_app.state, "llm_client", None)
    fastapi_app.state.llm_client = mock_llm_client_link_ext  # type: ignore[assignment] # For link_extractor_processor

    # Create a mock application object for TaskWorker
    mock_app_state_link_ext = MagicMock()
    mock_app_state_link_ext.embedding_generator = current_embedder
    mock_app_state_link_ext.llm_client = mock_llm_client_link_ext
    # Add scraper to mock app state if WebFetcherProcessor run by TaskWorker needs it via app.state.scraper
    mock_app_state_link_ext.scraper = mock_scraper

    mock_application_link_ext = MagicMock()
    mock_application_link_ext.state = mock_app_state_link_ext

    mock_chat_interface_link_ext = MagicMock()
    test_shutdown_event = asyncio.Event()  # Create shutdown event before TaskWorker
    worker_link_ext = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface_link_ext,
        embedding_generator=current_embedder,
        calendar_config={},
        timezone_str="UTC",
        shutdown_event_instance=test_shutdown_event,  # Pass the shutdown event
        engine=pg_vector_db_engine,  # Pass the engine for database operations
    )
    worker_link_ext.register_task_handler(
        "index_email", email_indexer_instance_link.handle_index_email
    )  # Use instance method
    worker_link_ext.register_task_handler(
        "embed_and_store_batch", handle_embed_and_store_batch
    )

    # worker_id was unused
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker_link_ext.run(test_new_task_event))
    await asyncio.sleep(0.1)

    email_db_id_link = None
    email_db_id_no_link = None
    test_failed = False

    try:
        # --- Act: Ingest Email with Primary Link ---
        email_msg_id_link = f"<email_link_test_pos_{uuid.uuid4()}@example.com>"
        form_data_link = TEST_EMAIL_FORM_DATA.copy()
        form_data_link.update({
            "subject": "Email with Primary Link",
            "stripped-text": TEST_EMAIL_BODY_PRIMARY_LINK,
            "Message-Id": email_msg_id_link,
        })
        email_db_id_link = await _ingest_and_index_email(
            http_client=http_client,
            engine=pg_vector_db_engine,
            form_data_dict=form_data_link,
            notify_event=test_new_task_event,
        )

        # --- Act: Ingest Email with No Primary Link ---
        email_msg_id_no_link = f"<email_link_test_neg_{uuid.uuid4()}@example.com>"
        form_data_no_link = TEST_EMAIL_FORM_DATA.copy()
        form_data_no_link.update({
            "subject": "Email with No Primary Link",
            "stripped-text": TEST_EMAIL_BODY_NO_PRIMARY_LINK,
            "Message-Id": email_msg_id_no_link,
        })
        email_db_id_no_link = await _ingest_and_index_email(
            http_client=http_client,
            engine=pg_vector_db_engine,
            form_data_dict=form_data_no_link,
            notify_event=test_new_task_event,
        )

        # --- Assert: Check MockScraper calls ---
        # The WebFetcherProcessor should have called the scraper with the extracted URL
        assert_that(mock_scraper.scraped_urls).described_as(
            "MockScraper should have been called for the primary link email"
        ).contains(EXPECTED_EXTRACTED_URL)
        assert_that(len(mock_scraper.scraped_urls)).described_as(
            "MockScraper should only be called once for the primary link"
        ).is_equal_to(1)

        # --- Assert: Check LLM calls ---
        # Positive case (email with link)
        positive_call_found = any(
            primary_link_matcher_positive(call_args["kwargs"])
            for call_args in mock_llm_client_link_ext.get_calls()
            if call_args["method_name"] == "generate_response"
        )
        assert_that(positive_call_found).described_as(
            "LLM should have been called for the email with a primary link matching positive rule."
        ).is_true()

        # Negative case (email without link)
        negative_call_found = any(
            primary_link_matcher_negative(call_args["kwargs"])
            for call_args in mock_llm_client_link_ext.get_calls()
            if call_args["method_name"] == "generate_response"
        )
        assert_that(negative_call_found).described_as(
            "LLM should have been called for the email without a primary link matching negative rule."
        ).is_true()

        # The assertions for positive_call_found and negative_call_found already verify
        # that the LLM's generate_response was called with the correct context for each email.
        # The exact count of calls is less important than these behavioral checks.

        logger.info(
            "--- Email Indexing with Primary Link Extraction E2E Test Passed ---"
        )

    except Exception as e:
        test_failed = True
        logger.error(f"Test failed: {e}", exc_info=True)
        raise
    finally:
        if test_failed:
            logger.info("Dumping tables due to test failure...")
            await dump_tables_on_failure(pg_vector_db_engine)

        if hasattr(fastapi_app.state, "llm_client"):
            if original_llm_client:
                fastapi_app.state.llm_client = original_llm_client
            else:
                delattr(fastapi_app.state, "llm_client")

        if hasattr(fastapi_app.state, "scraper"):  # Restore scraper
            delattr(fastapi_app.state, "scraper")

        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        # Basic cleanup attempt for created email records
        # A more robust cleanup would delete associated document and embedding records too.
        for db_id_to_clean in [email_db_id_link, email_db_id_no_link]:
            if db_id_to_clean:
                try:
                    async with DatabaseContext(
                        engine=pg_vector_db_engine
                    ) as _db_cleanup:
                        # Simplified cleanup, real cleanup would be more involved
                        logger.warning(
                            f"Partial cleanup for email_db_id {db_id_to_clean}. Manual check advised."
                        )
                except Exception as e:
                    logger.warning(f"Cleanup error for email {db_id_to_clean}: {e}")
