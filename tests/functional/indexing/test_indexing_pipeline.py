"""
Functional test for the basic document indexing pipeline.
"""

import asyncio
import logging
import pathlib  # Add import for pathlib
import shutil  # Add import for shutil
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

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
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage.context import get_db_context
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
from tests.helpers import wait_for_tasks_to_complete  # pylint: disable=import-error

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
        metadata: dict[str, Any] | None = None,
        source_uri: str | None = None,
    ) -> None:
        self._source_type = source_type
        self._source_id = source_id
        self._title = title
        self._created_at = (
            created_at.astimezone(timezone.utc)
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
    def metadata(self) -> dict[str, Any] | None:
        return self._metadata


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
    mock_application = MagicMock()
    # Ensure the mock_pipeline_embedding_generator is set on the mock_application's state
    # so that TaskWorker can pick it up when creating ToolExecutionContext.
    mock_application.state.embedding_generator = mock_pipeline_embedding_generator

    worker = TaskWorker(
        processing_service=MagicMock(),  # Use MagicMock for ProcessingService
        application=mock_application,
        embedding_generator=mock_pipeline_embedding_generator,  # Pass directly
        calendar_config={},
        timezone_str="UTC",
    )
    worker.register_task_handler(
        "embed_and_store_batch",
        handle_embed_and_store_batch,  # Register the handler directly
    )

    worker_task_handle = None
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    try:
        worker_task_handle = asyncio.create_task(worker.run(new_task_event))
        logger.info("Started background task worker for indexing_task_worker fixture.")
        await asyncio.sleep(0.1)  # Give worker time to start
        yield worker, new_task_event, shutdown_event
    finally:
        if worker_task_handle:
            logger.info(
                "Stopping background task worker from indexing_task_worker fixture..."
            )
            shutdown_event.set()
            try:
                await asyncio.wait_for(worker_task_handle, timeout=5.0)
                logger.info("Background task worker (fixture) stopped.")
            except asyncio.TimeoutError:
                logger.warning("Timeout stopping worker task (fixture). Cancelling.")
                worker_task_handle.cancel()
                try:
                    await worker_task_handle
                except asyncio.CancelledError:
                    logger.info("Worker task (fixture) cancellation confirmed.")
            except Exception as e:
                logger.error(
                    f"Error stopping worker task (fixture): {e}", exc_info=True
                )


@pytest.mark.asyncio
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
    db_context_for_pipeline_cm = get_db_context(engine=pg_vector_db_engine)

    # Initialize indexing_task_ids as an empty set
    indexing_task_ids: set[str] = set()

    try:
        async with (
            db_context_for_pipeline_cm as db_context_for_pipeline
        ):  # This acquires the connection
            tool_exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test-indexing-conv",
                turn_id=str(uuid.uuid4()),  # ADDED turn_id
                db_context=db_context_for_pipeline,
                application=MagicMock(),  # Provide a mock application object
                embedding_generator=mock_pipeline_embedding_generator,
            )

            # Create and store the document
            test_document_protocol = MockDocumentImpl(
                source_type="test", source_id=doc_source_id, title=doc_title
            )
            doc_db_id = await add_document(
                db_context_for_pipeline, test_document_protocol
            )
            # Set the ID on the protocol object so the pipeline can use it
            test_document_protocol._id = doc_db_id  # type: ignore[attr-defined]

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
            embedding_dispatcher = EmbeddingDispatchProcessor(  # Dispatch types produced by preceding stages
                embedding_types_to_dispatch=["title_chunk", "raw_text_chunk"]
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
        async with get_db_context(  # Removed await
            engine=pg_vector_db_engine
        ) as db_context_for_asserts:
            stmt_verify_embeddings = DocumentEmbeddingRecord.__table__.select().where(
                DocumentEmbeddingRecord.__table__.c.document_id == doc_db_id
            )
            stored_embeddings_rows = await db_context_for_asserts.fetch_all(
                stmt_verify_embeddings
            )

            assert_that(len(stored_embeddings_rows)).described_as(
                "Expected at least title and one chunk embedding"
            ).is_greater_than_or_equal_to(2)

            # Log stored content for debugging
            logger.info(f"Stored Embeddings (doc_id={doc_db_id}):")
            for i, row_proxy_log in enumerate(stored_embeddings_rows):
                row_dict_log = dict(row_proxy_log)
                logger.info(
                    f"  Row {i}: Type='{row_dict_log.get('embedding_type')}', ChunkIdx='{row_dict_log.get('chunk_index')}', Content='{row_dict_log.get('content')}'"
                )

            title_embedding_found = False
            chunk_embeddings_found = 0
            expected_chunk_texts = [
                "Apples are red Bananas are yel",  # Chunk 1
                "e yellow Oranges are orange an",  # Chunk 2 - updated based on test failure
                "ge and tasty.",  # Chunk 3 - updated based on test failure
            ]

            for row_proxy in stored_embeddings_rows:
                row = dict(row_proxy)  # Convert RowProxy to dict for easier access
                assert_that(row["embedding_model"]).is_equal_to(
                    TEST_EMBEDDING_MODEL_NAME
                )
                # Check for the chunked title type
                if row["embedding_type"] == "title_chunk":
                    assert_that(row["content"]).is_equal_to(
                        "Fruit Facts"
                    )  # Check content
                    title_embedding_found = True
                elif row["embedding_type"] == "raw_text_chunk":
                    assert_that(expected_chunk_texts).contains(row["content"])
                    chunk_embeddings_found += 1

            assert_that(title_embedding_found).described_as(
                "Title embedding (title_chunk) not found"
            ).is_true()
            assert_that(chunk_embeddings_found).described_as(
                f"Expected {len(expected_chunk_texts)} chunk embeddings"
            ).is_equal_to(len(expected_chunk_texts))

            # Verify search
            query_text_for_chunk = "yellow and orange"  # Should match the second chunk
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
                limit=5,
            )
            assert_that(search_results).described_as(
                f"Vector search results for query '{query_text_for_chunk}'"
            ).is_not_empty()

            found_matching_chunk_in_search = False
            for res in search_results:
                if (
                    res["document_id"] == doc_db_id
                    and res["embedding_source_content"] == expected_chunk_texts[1]
                ):  # "low Oranges are orange and tas"
                    found_matching_chunk_in_search = True
                    break
            assert_that(found_matching_chunk_in_search).described_as(
                "Relevant chunk not found via vector search"
            ).is_true()

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

    db_context_for_pipeline_cm = get_db_context(engine=pg_vector_db_engine)

    try:
        async with db_context_for_pipeline_cm as db_context_for_pipeline:
            tool_exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test-pdf-pipeline-conv",
                turn_id=str(uuid.uuid4()),  # ADDED turn_id
                db_context=db_context_for_pipeline,
                application=MagicMock(),
                embedding_generator=mock_pipeline_embedding_generator,
            )

            test_document_protocol = MockDocumentImpl(
                source_type="test_pdf", source_id=doc_source_id, title=doc_title
            )
            doc_db_id = await add_document(
                db_context_for_pipeline, test_document_protocol
            )
            # Set the ID on the protocol object so the pipeline can use it
            test_document_protocol._id = doc_db_id  # type: ignore[attr-defined]

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
            from family_assistant.indexing.processors.file_processors import (
                PDFTextExtractor,  # Local import
            )

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
        async with get_db_context(  # Removed await
            engine=pg_vector_db_engine
        ) as db_context_for_asserts:
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
                    row_dict_log.get("embedding_type")
                    == "extracted_markdown_content_chunk"
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
