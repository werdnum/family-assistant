"""
Functional test for the basic document indexing pipeline.
"""

import asyncio
import logging
import pathlib  # Add import for pathlib
import shutil  # Add import for shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio  # For async fixtures
from assertpy import assert_that  # For better assertions
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import (
    HashingWordEmbeddingGenerator,  # Added
)
from family_assistant.indexing.pipeline import IndexableContent, IndexingPipeline
from family_assistant.indexing.processors.dispatch_processors import (
    EmbeddingDispatchProcessor,
)
from family_assistant.indexing.processors.file_processors import PDFTextExtractor
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.storage.vector import (
    Document as DocumentProtocol,
)
from family_assistant.storage.vector import (
    DocumentEmbeddingRecord,
    add_document,
    get_document_by_source_id,
    query_vectors,
)
from family_assistant.task_worker import TaskWorker  # For running the task worker
from family_assistant.tools.types import ToolExecutionContext
from tests.conftest import cleanup_task_worker
from tests.helpers import wait_for_tasks_to_complete


def _create_mock_processing_service() -> MagicMock:
    """Create a mock ProcessingService with required attributes."""
    mock = MagicMock()
    return mock


logger = logging.getLogger(__name__)

TEST_EMBEDDING_MODEL_NAME = "test-indexing-model"
TEST_EMBEDDING_DIMENSION = 10  # Small dimension for mock


class MockDocumentImpl(DocumentProtocol):
    """Simple implementation of the Document protocol for test data."""

    def __init__(
        self,
        source_type: str,
        source_id: str,
        title: str | None = None,
        created_at: datetime | None = None,
        # ast-grep-ignore: no-dict-any - mock document metadata has dynamic third-party fields
        metadata: dict[str, Any] | None = None,
        source_uri: str | None = None,
    ) -> None:
        self._source_type = source_type
        self._source_id = source_id
        self._title = title
        self._created_at = (
            created_at.astimezone(UTC)
            if created_at and created_at.tzinfo is None
            else created_at
        )
        self._metadata = metadata or {}
        self._source_uri = source_uri
        self._id: int | None = None  # Add an internal attribute for ID

    @property
    def id(self) -> int | None:
        return self._id

    @property
    def source_type(self) -> str:
        return self._source_type

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_uri(self) -> str | None:
        return self._source_uri

    @property
    def title(self) -> str | None:
        return self._title

    @property
    def created_at(self) -> datetime | None:
        return self._created_at

    @property
    # ast-grep-ignore: no-dict-any - mock document metadata has dynamic third-party fields
    def metadata(self) -> dict[str, Any] | None:
        return self._metadata

    @property
    def file_path(self) -> str | None:
        return None  # Mock documents don't have file paths by default

    @property
    def visibility_labels(self) -> list[str] | None:
        return None


@pytest_asyncio.fixture(scope="function")
async def mock_pipeline_embedding_generator() -> (
    HashingWordEmbeddingGenerator
):  # Changed return type
    """
    Provides a HashingWordEmbeddingGenerator instance for the pipeline test.
    """
    generator = HashingWordEmbeddingGenerator(
        model_name=TEST_EMBEDDING_MODEL_NAME,
        dimensionality=TEST_EMBEDDING_DIMENSION,
    )
    return generator


@pytest_asyncio.fixture(scope="function")
async def indexing_task_worker(
    pg_vector_db_engine: AsyncEngine,  # Depends on the DB engine
    mock_pipeline_embedding_generator: HashingWordEmbeddingGenerator,  # Depends on the mock generator
) -> AsyncIterator[tuple[TaskWorker, asyncio.Event, asyncio.Event]]:
    """
    Sets up and tears down a TaskWorker instance configured for indexing tasks.
    Yields the worker, new_task_event, and shutdown_event.
    """
    # mock_application is no longer needed here as embedding_generator is passed directly to TaskWorker
    # and ToolExecutionContext.

    mock_chat_interface_for_worker = MagicMock()
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),  # Use MagicMock for ProcessingService
        chat_interface=mock_chat_interface_for_worker,
        embedding_generator=mock_pipeline_embedding_generator,  # Pass directly
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        shutdown_event_instance=shutdown_event,
        engine=pg_vector_db_engine,  # Pass the database engine
    )
    worker.register_task_handler(
        "embed_and_store_batch",
        handle_embed_and_store_batch,  # Register the handler directly
    )

    worker_task_handle = asyncio.create_task(worker.run(new_task_event))
    logger.info("Started background task worker for indexing_task_worker fixture.")
    try:
        yield worker, new_task_event, shutdown_event
    finally:
        await cleanup_task_worker(
            worker_task_handle,
            shutdown_event,
            new_task_event,
            test_name="indexing_task_worker",
        )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_indexing_pipeline_e2e(
    pg_vector_db_engine: AsyncEngine,
    mock_pipeline_embedding_generator: HashingWordEmbeddingGenerator,  # Get the generator instance
    indexing_task_worker: tuple[
        TaskWorker, asyncio.Event, asyncio.Event
    ],  # Use the new fixture
) -> None:
    """
    End-to-end test for a basic indexing pipeline:
    1. Creates a document.
    2. Runs it through TitleExtractor -> TextChunker -> EmbeddingDispatchProcessor.
    3. Verifies embeddings for title and chunks are stored in the DB.
    4. Verifies the content can be retrieved via vector search.
    """
    # Clean up any leftover tasks from previous tests to ensure isolation
    db_ctx = Database(engine=pg_vector_db_engine)
    await db_ctx.execute(
        tasks_table.delete().where(tasks_table.c.task_type == "embed_and_store_batch")
    )

    # --- Arrange ---
    doc_content = "Apples are red. Bananas are yellow. Oranges are orange and tasty."
    doc_title = "Fruit Facts"
    doc_source_id = f"test-doc-{uuid.uuid4()}"

    # Unpack worker and events from the fixture
    _worker, test_new_task_event, _test_shutdown_event = indexing_task_worker

    # Setup TaskWorker
    # mock_application is now created inside the fixture if needed by the worker
    # The worker itself is part of the `indexing_task_worker` fixture's return value

    # ToolExecutionContext for the pipeline run (uses real enqueue_task)
    # This db_context is for the pipeline's direct DB operations (like add_document)
    # and for the enqueue_task call within EmbeddingDispatchProcessor.
    db_context_for_pipeline_cm = Database(engine=pg_vector_db_engine)

    # Initialize indexing_task_ids as an empty set
    indexing_task_ids: set[str] = set()

    try:
        db_context_for_pipeline = db_context_for_pipeline_cm
        tool_exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-indexing-conv",
            user_name="IndexingTestUser",  # Added
            turn_id=str(uuid.uuid4()),  # ADDED turn_id
            db_context=db_context_for_pipeline,
            task_priority=TaskPriority.INTERACTIVE,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            chat_interface=MagicMock(),  # Provide a mock ChatInterface
            embedding_generator=mock_pipeline_embedding_generator,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        # Create and store the document
        test_document_protocol = MockDocumentImpl(
            source_type="test", source_id=doc_source_id, title=doc_title
        )
        doc_db_id = await add_document(db_context_for_pipeline, test_document_protocol)
        # Set the ID on the protocol object so the pipeline can use it
        test_document_protocol._id = doc_db_id

        original_doc_record = await get_document_by_source_id(
            db_context_for_pipeline, doc_source_id
        )
        assert_that(original_doc_record).is_not_none()
        assert original_doc_record is not None  # For type checker
        assert_that(original_doc_record.id).is_equal_to(doc_db_id)

        # Initial IndexableContent
        initial_content = IndexableContent(
            embedding_type="raw_text",
            source_processor="test_setup",
            content=doc_content,
            mime_type="text/plain",
            metadata={"original_filename": "test_doc.txt"},
        )

        # Setup Pipeline
        title_extractor = TitleExtractor()
        text_chunker = TextChunker(
            chunk_size=30, chunk_overlap=5
        )  # Small for predictable chunks
        # Ensure the dispatch processor handles the types generated by previous stages
        embedding_dispatcher = (
            EmbeddingDispatchProcessor(  # Dispatch types produced by preceding stages
                embedding_types_to_dispatch=["title_chunk", "raw_text_chunk"]
            )
        )
        pipeline = IndexingPipeline(
            processors=[title_extractor, text_chunker, embedding_dispatcher],
            config={},
        )

        # --- Act ---
        logger.info(
            f"Running indexing pipeline for document ID {doc_db_id} ({doc_source_id})..."
        )
        await pipeline.run(
            [initial_content],
            test_document_protocol,
            tool_exec_context,  # Pass protocol object
        )

        # Signal worker and wait for task completion
        test_new_task_event.set()
        # Wait for all tasks to complete as we are not tracking specific IDs here
        logger.info(
            f"Waiting for all enqueued tasks to complete for document ID {doc_db_id}..."
        )
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            timeout_seconds=20.0,
        )
        logger.info(f"Tasks {indexing_task_ids} reported as complete.")

        # --- Assert ---
        # Verify embeddings in DB
        # Use a new context for assertions as the previous one is closed
        db_context_for_asserts = Database(engine=pg_vector_db_engine)  # Removed await
        stmt_verify_embeddings = DocumentEmbeddingRecord.__table__.select().where(
            DocumentEmbeddingRecord.__table__.c.document_id == doc_db_id
        )
        stored_embeddings_rows = await db_context_for_asserts.fetch_all(
            stmt_verify_embeddings
        )

        expected_chunk_texts = [
            "Apples are red Bananas are yel",
            "e yellow Oranges are orange an",
            "ge and tasty.",
        ]
        stored_embeddings = sorted(
            (row["embedding_type"], row["content"], row["embedding_model"])
            for row in stored_embeddings_rows
        )
        expected_embeddings = sorted(
            [("title_chunk", doc_title, TEST_EMBEDDING_MODEL_NAME)]
            + [
                ("raw_text_chunk", chunk_text, TEST_EMBEDDING_MODEL_NAME)
                for chunk_text in expected_chunk_texts
            ]
        )
        assert_that(stored_embeddings).described_as(
            f"Stored embeddings for document {doc_db_id}"
        ).is_equal_to(expected_embeddings)

        # At 10 hashed dimensions, word overlap does not reliably decide the
        # ranking (unrelated words share buckets), so query with the chunk's own
        # text: its stored embedding is at distance zero and must rank first.
        query_text_for_chunk = expected_chunk_texts[1]
        query_vector_result = (
            await mock_pipeline_embedding_generator.generate_embeddings([
                query_text_for_chunk
            ])
        )
        query_embedding = query_vector_result.embeddings[0]

        search_results = await query_vectors(
            db_context_for_asserts,
            query_embedding,
            TEST_EMBEDDING_MODEL_NAME,
            limit=1,
        )
        assert_that([
            (res["document_id"], res["embedding_source_content"])
            for res in search_results
        ]).described_as(
            f"Nearest vector search result for query '{query_text_for_chunk}'"
        ).is_equal_to([(doc_db_id, query_text_for_chunk)])

        logger.info("Indexing pipeline E2E test passed.")

    finally:
        # Worker lifecycle is now managed by the `indexing_task_worker` fixture's teardown

        # Clean up tasks
        # The wait_for_tasks_to_complete helper doesn't return task_ids easily
        # For this test, we'll rely on the task worker processing them and them being marked 'done'
        # or 'failed'. Manual cleanup of specific task IDs is tricky without tracking them.
        # If specific task ID cleanup is needed, the test would have to capture them
        # when EmbeddingDispatchProcessor enqueues them.
        logger.info("Test finished, task cleanup relies on worker processing.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_indexing_pipeline_pdf_processing(
    pg_vector_db_engine: AsyncEngine,
    mock_pipeline_embedding_generator: HashingWordEmbeddingGenerator,
    indexing_task_worker: tuple[TaskWorker, asyncio.Event, asyncio.Event],
    tmp_path: pathlib.Path,  # Pytest fixture for temporary directory
) -> None:
    """
    Tests the indexing pipeline with PDFTextExtractor.
    1. Creates a dummy PDF file.
    2. Runs an IndexableContent item for this PDF through a pipeline including PDFTextExtractor.
    3. Verifies that text is extracted and embedding tasks are created for the extracted content.
    """
    # Clean up any leftover tasks from previous tests to ensure isolation
    db_ctx = Database(engine=pg_vector_db_engine)
    await db_ctx.execute(
        tasks_table.delete().where(tasks_table.c.task_type == "embed_and_store_batch")
    )

    # --- Arrange ---
    # Create a dummy PDF file for testing (or copy a test PDF)
    # For simplicity, we'll use the existing test_doc.pdf from tests/data
    # and copy it to a temporary location for this test run.
    # The data directory is expected to be at tests/data, so we go up three levels from the current file.
    source_pdf_path = (
        pathlib.Path(__file__).parent.parent.parent / "data" / "test_doc.pdf"
    )
    assert source_pdf_path.exists(), f"Test PDF {source_pdf_path} not found"

    test_pdf_filename = "test_pipeline_doc.pdf"
    temp_pdf_path = tmp_path / test_pdf_filename
    shutil.copy(source_pdf_path, temp_pdf_path)

    doc_source_id = f"test-pdf-pipeline-{uuid.uuid4()}"
    doc_title = "Pipeline PDF Test"

    _worker, test_new_task_event, _test_shutdown_event = indexing_task_worker

    db_context_for_pipeline_cm = Database(engine=pg_vector_db_engine)

    try:
        db_context_for_pipeline = db_context_for_pipeline_cm
        tool_exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-pdf-pipeline-conv",
            user_name="PDFIndexingTestUser",  # Added
            turn_id=str(uuid.uuid4()),  # ADDED turn_id
            db_context=db_context_for_pipeline,
            task_priority=TaskPriority.INTERACTIVE,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            chat_interface=MagicMock(),  # Provide a mock ChatInterface
            embedding_generator=mock_pipeline_embedding_generator,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        test_document_protocol = MockDocumentImpl(
            source_type="test_pdf", source_id=doc_source_id, title=doc_title
        )
        doc_db_id = await add_document(db_context_for_pipeline, test_document_protocol)
        # Set the ID on the protocol object so the pipeline can use it
        test_document_protocol._id = doc_db_id

        original_doc_record = await get_document_by_source_id(
            db_context_for_pipeline, doc_source_id
        )
        assert_that(original_doc_record).is_not_none()
        assert original_doc_record is not None  # For type checker

        # Initial IndexableContent for the PDF file
        initial_pdf_content = IndexableContent(
            embedding_type="original_document_file",
            source_processor="test_pdf_setup",
            mime_type="application/pdf",
            ref=str(temp_pdf_path),  # Path to the test PDF
            metadata={"original_filename": test_pdf_filename},
        )

        # Setup Pipeline with PDFTextExtractor

        pdf_extractor = PDFTextExtractor()
        # TextChunker to process the markdown output of PDFTextExtractor
        text_chunker = TextChunker(
            chunk_size=500,  # Adjust as needed for test_doc.pdf content
            chunk_overlap=50,
        )
        embedding_dispatcher = EmbeddingDispatchProcessor(
            embedding_types_to_dispatch=[
                "extracted_markdown_content_chunk"
            ]  # Expecting chunks from markdown
        )
        pipeline = IndexingPipeline(
            processors=[pdf_extractor, text_chunker, embedding_dispatcher],
            config={},
        )

        # --- Act ---
        logger.info(
            f"Running PDF indexing pipeline for document ID {doc_db_id} ({doc_source_id})..."
        )
        await pipeline.run(
            [initial_pdf_content],
            test_document_protocol,
            tool_exec_context,  # Pass protocol object
        )

        test_new_task_event.set()
        logger.info(
            f"Waiting for PDF processing tasks to complete for document ID {doc_db_id}..."
        )
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            timeout_seconds=25.0,  # PDF processing might take a bit longer
        )
        logger.info(
            f"PDF processing tasks reported as complete for document ID {doc_db_id}."
        )

        # --- Assert ---
        db_context_for_asserts = Database(engine=pg_vector_db_engine)  # Removed await
        stmt_verify_embeddings = DocumentEmbeddingRecord.__table__.select().where(
            DocumentEmbeddingRecord.__table__.c.document_id == doc_db_id
        )
        stored_embeddings_rows = await db_context_for_asserts.fetch_all(
            stmt_verify_embeddings
        )

        assert_that(len(stored_embeddings_rows)).described_as(
            "Expected embeddings from PDF extracted content"
        ).is_greater_than_or_equal_to(1)

        logger.info(f"Stored Embeddings from PDF (doc_id={doc_db_id}):")
        found_expected_content = False
        # Known phrase from test_doc.md (which test_doc.pdf is generated from)
        # This phrase should be specific enough and likely to survive chunking.
        # From "Software updates are a common and crucial aspect of using digital devices"
        known_phrase_in_pdf = "crucial aspect of using digital devices"

        for i, row_proxy_log in enumerate(stored_embeddings_rows):
            row_dict_log = dict(row_proxy_log)
            logger.info(
                f"  Row {i}: Type='{row_dict_log.get('embedding_type')}', ChunkIdx='{row_dict_log.get('chunk_index')}', Content='{str(row_dict_log.get('content'))[:100]}...'"
            )
            if (
                row_dict_log.get("embedding_type") == "extracted_markdown_content_chunk"
                and row_dict_log.get("content")
                and known_phrase_in_pdf in str(row_dict_log.get("content"))
            ):
                found_expected_content = True

        assert_that(found_expected_content).described_as(
            f"Known phrase '{known_phrase_in_pdf}' not found in any extracted PDF content chunks."
        ).is_true()

        # Verify search (optional, but good for E2E feel)
        # This requires knowing/mocking the embedding for the known phrase
        # For simplicity, we'll skip vector search for this specific pipeline unit test
        # and focus on the presence of processed content.

        logger.info("PDF indexing pipeline test passed.")

    finally:
        logger.info(
            "Test PDF processing finished, task cleanup relies on worker processing."
        )
        # tmp_path fixture handles cleanup of the temp_pdf_path
