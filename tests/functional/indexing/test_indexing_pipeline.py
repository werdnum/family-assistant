"""
Functional test for the basic document indexing pipeline.
"""
import pytest
import asyncio
import uuid
import pytest_asyncio # For async fixtures
import logging
from assertpy import assert_that # For better assertions
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple, Awaitable, Callable
from unittest.mock import MagicMock

import numpy as np
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.vector import (
    add_document,
    get_document_by_source_id,
    query_vectors,
    DocumentRecord,
    DocumentEmbeddingRecord,
    Document as DocumentProtocol,
)
from family_assistant.storage.tasks import tasks_table # For querying tasks
from sqlalchemy import select # For selecting tasks
from family_assistant.embeddings import MockEmbeddingGenerator, EmbeddingGenerator, EmbeddingResult
from family_assistant.indexing.pipeline import IndexingPipeline, IndexableContent
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.processors.dispatch_processors import EmbeddingDispatchProcessor
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.task_worker import TaskWorker # For running the task worker
from tests.helpers import wait_for_tasks_to_complete # For waiting for task completion

logger = logging.getLogger(__name__)

TEST_EMBEDDING_MODEL_NAME = "test-indexing-model"
TEST_EMBEDDING_DIMENSION = 10 # Small dimension for mock

class MockDocumentImpl(DocumentProtocol):
    """Simple implementation of the Document protocol for test data."""

    def __init__(
        self,
        source_type: str,
        source_id: str,
        title: Optional[str] = None,
        created_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
    ):
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

    @property
    def source_type(self) -> str:
        return self._source_type

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_uri(self) -> Optional[str]:
        return self._source_uri

    @property
    def title(self) -> Optional[str]:
        return self._title

    @property
    def created_at(self) -> Optional[datetime]:
        return self._created_at

    @property
    def metadata(self) -> Optional[Dict[str, Any]]:
        return self._metadata


# Wrapper class to hold embedding_generator and provide a compliant task handler method
class EmbeddingTaskHandlerWrapper:
    def __init__(self, embedding_generator: EmbeddingGenerator):
        self.embedding_generator = embedding_generator

    async def handle_task(self, context: ToolExecutionContext, payload: Dict[str, Any]):
        # Call the original handle_embed_and_store_batch, adapting arguments
        await handle_embed_and_store_batch(
            db_context=context.db_context, # Extract DatabaseContext from ToolExecutionContext
            payload=payload,
            embedding_generator=self.embedding_generator
        )


@pytest_asyncio.fixture(scope="function")
async def mock_pipeline_embedding_generator() -> MockEmbeddingGenerator:
    """
    Provides a MockEmbeddingGenerator instance specifically for the pipeline test,
    which dynamically populates its embedding_map.
    """
    # --- Arrange ---
    # doc_content and doc_title are specific to the test logic, not the generator itself.

    # Mock Embedding Generator
    # It's important that different texts map to different (but consistent) vectors.
    # For simplicity, we'll make it return a vector based on sum of char ords.
    def generate_simple_vector(text: str) -> List[float]:
        # Crude but deterministic vector generation for testing
        base_val = sum(ord(c) for c in text) % 1000
        return [float(base_val + i) for i in range(TEST_EMBEDDING_DIMENSION)]

    embedding_map = {} # Will be populated by the generator as it sees texts

    class TestSpecificMockEmbeddingGenerator(MockEmbeddingGenerator):
        async def generate_embeddings(self, texts: List[str]) -> EmbeddingResult:
            # Ensure this returns unique vectors for unique texts during the test
            for text in texts:
                if text not in self.embedding_map:
                    self.embedding_map[text] = generate_simple_vector(text)
            return await super().generate_embeddings(texts)

    generator = TestSpecificMockEmbeddingGenerator(
        embedding_map=embedding_map, # Start with empty, will populate
        model_name=TEST_EMBEDDING_MODEL_NAME,
        default_embedding=[0.0] * TEST_EMBEDDING_DIMENSION
    )
    return generator


@pytest_asyncio.fixture(scope="function")
async def indexing_task_worker(
    pg_vector_db_engine: AsyncEngine, # Depends on the DB engine
    mock_pipeline_embedding_generator: MockEmbeddingGenerator # Depends on the mock generator
):
    """
    Sets up and tears down a TaskWorker instance configured for indexing tasks.
    Yields the worker, new_task_event, and shutdown_event.
    """
    mock_application = MagicMock()
    worker = TaskWorker(
        processing_service=None,
        application=mock_application,
        calendar_config={},
        timezone_str="UTC",
    )
    embedding_task_executor = EmbeddingTaskHandlerWrapper(mock_pipeline_embedding_generator)
    worker.register_task_handler("embed_and_store_batch", embedding_task_executor.handle_task)

    worker_task_handle = None
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    try:
        worker_task_handle = asyncio.create_task(worker.run(new_task_event))
        logger.info("Started background task worker for indexing_task_worker fixture.")
        await asyncio.sleep(0.1) # Give worker time to start
        yield worker, new_task_event, shutdown_event
    finally:
        if worker_task_handle:
            logger.info("Stopping background task worker from indexing_task_worker fixture...")
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
                logger.error(f"Error stopping worker task (fixture): {e}", exc_info=True)


@pytest.mark.asyncio
async def test_indexing_pipeline_e2e(
    pg_vector_db_engine: AsyncEngine,
    mock_pipeline_embedding_generator: MockEmbeddingGenerator, # Get the generator instance
    indexing_task_worker: Tuple[TaskWorker, asyncio.Event, asyncio.Event] # Use the new fixture
):
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
    db_context_for_pipeline = await get_db_context(engine=pg_vector_db_engine)

    # Initialize indexing_task_ids as an empty set
    indexing_task_ids: set[str] = set()

    try:
        async with db_context_for_pipeline:  # This acquires the connection
            tool_exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test-indexing-conv",
                db_context=db_context_for_pipeline,
                calendar_config={},
                application=MagicMock(),  # Provide a mock application object
            )

            # Create and store the document
            test_document_protocol = MockDocumentImpl(
                source_type="test", source_id=doc_source_id, title=doc_title
            )
            doc_db_id = await add_document(db_context_for_pipeline, test_document_protocol)
            original_doc_record = await get_document_by_source_id(db_context_for_pipeline, doc_source_id)
            assert_that(original_doc_record).is_not_none()
            assert_that(original_doc_record.id).is_equal_to(doc_db_id)

            # Initial IndexableContent
            initial_content = IndexableContent(
                embedding_type="raw_text",
                source_processor="test_setup",
                content=doc_content,
                mime_type="text/plain",
                metadata={"original_filename": "test_doc.txt"}
            )

            # Setup Pipeline
            title_extractor = TitleExtractor()
            text_chunker = TextChunker(chunk_size=30, chunk_overlap=5) # Small for predictable chunks
            # Ensure the dispatch processor handles the types generated by previous stages
            embedding_dispatcher = EmbeddingDispatchProcessor(
                embedding_types_to_dispatch=["title", "raw_text_chunk"]
            )
            pipeline = IndexingPipeline(
                processors=[title_extractor, text_chunker, embedding_dispatcher], config={}
            )

            # --- Act ---
            logger.info(f"Running indexing pipeline for document ID {doc_db_id} ({doc_source_id})...")
            await pipeline.run(initial_content, original_doc_record, tool_exec_context)

        # Signal worker and wait for task completion
        test_new_task_event.set()
        # Wait for all tasks to complete as we are not tracking specific IDs here
        logger.info(f"Waiting for all enqueued tasks to complete for document ID {doc_db_id}...")
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            # task_ids=indexing_task_ids, # Removed to wait for all tasks
            timeout_seconds=20.0,
        )
        logger.info(f"Tasks {indexing_task_ids} reported as complete.")

        # --- Assert ---
        # Verify embeddings in DB
        # Use a new context for assertions as the previous one is closed
        async with await get_db_context(engine=pg_vector_db_engine) as db_context_for_asserts:

            stmt_verify_embeddings = DocumentEmbeddingRecord.__table__.select().where(DocumentEmbeddingRecord.__table__.c.document_id == doc_db_id)
            stored_embeddings_rows = await db_context_for_asserts.fetch_all(stmt_verify_embeddings)

            assert_that(len(stored_embeddings_rows)).described_as("Expected at least title and one chunk embedding").is_greater_than_or_equal_to(2)

            title_embedding_found = False
            chunk_embeddings_found = 0
            expected_chunk_texts = [ # Based on chunker logic and test content/size
                "Apples are red. Bananas are", # Chunk 1
                "anas are yellow. Oranges are", # Chunk 2 (overlap "anas ")
                "ges are orange and tasty." # Chunk 3 (overlap "ges a")
            ]

            for row_proxy in stored_embeddings_rows:
                row = dict(row_proxy) # Convert RowProxy to dict for easier access
                assert_that(row["embedding_model"]).is_equal_to(TEST_EMBEDDING_MODEL_NAME)
                if row["embedding_type"] == "title":
                    assert_that(row["content"]).is_equal_to(doc_title)
                    title_embedding_found = True
                elif row["embedding_type"] == "raw_text_chunk":
                    assert_that(expected_chunk_texts).contains(row["content"])
                    chunk_embeddings_found += 1

            assert_that(title_embedding_found).described_as("Title embedding not found").is_true()
            assert_that(chunk_embeddings_found).described_as(
                f"Expected {len(expected_chunk_texts)} chunk embeddings"
            ).is_equal_to(len(expected_chunk_texts))

            # Verify search
            query_text_for_chunk = "yellow bananas" # Should match chunk 2
            query_vector_result = await mock_pipeline_embedding_generator.generate_embeddings([query_text_for_chunk])
            query_embedding = query_vector_result.embeddings[0]

            search_results = await query_vectors(
                db_context_for_asserts, query_embedding, TEST_EMBEDDING_MODEL_NAME, limit=5
            )
            assert_that(search_results).described_as("Vector search results").is_not_empty()

            found_matching_chunk_in_search = False
            for res in search_results:
                if res["document_id"] == doc_db_id and "yellow. Oranges are" in res["embedding_source_content"]:
                    found_matching_chunk_in_search = True
                    break
            assert_that(found_matching_chunk_in_search).described_as("Relevant chunk not found via vector search").is_true()

        logger.info("Indexing pipeline E2E test passed.")

    finally:
        # Worker lifecycle is now managed by the `indexing_task_worker` fixture's teardown

        # Clean up tasks
        if indexing_task_ids:
            try:
                async with await get_db_context(engine=pg_vector_db_engine) as db_cleanup:
                    delete_stmt = tasks_table.delete().where(tasks_table.c.task_id.in_(indexing_task_ids))
                    await db_cleanup.execute_with_retry(delete_stmt)
                    logger.info(f"Cleaned up test tasks: {indexing_task_ids}")
            except Exception as cleanup_err:
                logger.warning(f"Error during test task cleanup: {cleanup_err}")
