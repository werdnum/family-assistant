"""
End-to-end functional tests for the document re-indexing pipeline.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.document_indexer import DocumentIndexer
from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import TaskWorker, handle_reindex_document
from family_assistant.utils.scraping import MockScraper, ScrapeResult
from family_assistant.web.app_creator import app as fastapi_app
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _create_mock_processing_service() -> MagicMock:
    """Create a mock ProcessingService with required attributes."""
    mock = MagicMock()
    return mock


logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_EMBEDDING_MODEL = "mock-reindex-model"
TEST_EMBEDDING_DIMENSION = 16

# --- Test Data ---
TEST_URL = "https://example.com/reindex-test-page"
BUGGY_CONTENT = "<html><body><p>Scraping failed.</p></body></html>"
CORRECT_CONTENT_MARKDOWN = """# Correct Page Title

This is the correct content that should be indexed after the fix.
It contains keywords like `re-indexed` and `successfully`.
"""
CORRECT_TITLE = "Correct Page Title"
EXPECTED_CHUNK = "# Correct Page Title This is the correct content that should be indexed after the fix. It contains keywords like `re-indexed` and `successfully`."
TEST_QUERY = "successfully re-indexed content"


# --- Fixtures ---


@pytest_asyncio.fixture(scope="function")
async def mock_embedding_generator() -> MockEmbeddingGenerator:
    """Provides a function-scoped mock embedding generator."""
    correct_chunk_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.5
    ).tolist()
    query_embedding = (
        np.array(correct_chunk_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()

    embedding_map = {
        EXPECTED_CHUNK: correct_chunk_embedding,
        TEST_QUERY: query_embedding,
    }
    generator = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=TEST_EMBEDDING_MODEL,
        dimensions=TEST_EMBEDDING_DIMENSION,
        default_embedding_behavior="fixed_default",
        fixed_default_embedding=np.zeros(TEST_EMBEDDING_DIMENSION).tolist(),
    )
    return generator


@pytest_asyncio.fixture(scope="function")
async def http_client(
    pg_vector_db_engine: AsyncEngine,
    mock_embedding_generator: MockEmbeddingGenerator,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Provides a test client for the FastAPI application."""
    # Store original state
    original_embedding_generator = getattr(
        fastapi_app.state, "embedding_generator", None
    )
    original_database_engine = getattr(fastapi_app.state, "database_engine", None)
    original_config = getattr(fastapi_app.state, "config", {})

    # Set up test state
    fastapi_app.state.embedding_generator = mock_embedding_generator
    fastapi_app.state.database_engine = pg_vector_db_engine
    test_config = original_config.copy() if isinstance(original_config, dict) else {}
    test_config["document_storage_path"] = "/tmp/reindex_test"
    fastapi_app.state.config = test_config

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Restore original state
    if original_config:
        fastapi_app.state.config = original_config
    else:
        delattr(fastapi_app.state, "config")

    if original_database_engine is not None:
        fastapi_app.state.database_engine = original_database_engine
    elif hasattr(fastapi_app.state, "database_engine"):
        delattr(fastapi_app.state, "database_engine")

    if original_embedding_generator is not None:
        fastapi_app.state.embedding_generator = original_embedding_generator
    elif hasattr(fastapi_app.state, "embedding_generator"):
        delattr(fastapi_app.state, "embedding_generator")


async def _helper_handle_embed_and_store_batch(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    exec_context: "ToolExecutionContext",
    payload: dict[str, Any],
) -> None:
    """Helper task handler for embedding and storing batches."""
    db_context = exec_context.db_context
    embedding_generator = exec_context.embedding_generator
    document_id = payload["document_id"]
    texts_to_embed: list[str] = payload["texts_to_embed"]
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    embedding_metadata_list: list[dict[str, Any]] = payload["embedding_metadata_list"]

    if not texts_to_embed or not embedding_generator:
        return

    embedding_result = await embedding_generator.generate_embeddings(texts_to_embed)

    for i, vector in enumerate(embedding_result.embeddings):
        meta = embedding_metadata_list[i]
        await db_context.vector.add_embedding(
            document_id=document_id,
            chunk_index=meta.get("chunk_index", 0),
            embedding_type=meta["embedding_type"],
            embedding=vector,
            embedding_model=embedding_result.model_name,
            content=texts_to_embed[i],
            embedding_doc_metadata=meta.get("original_content_metadata"),
        )


# --- Test Function ---


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_reindex_document_e2e(
    pg_vector_db_engine: AsyncEngine,
    http_client: httpx.AsyncClient,
    mock_embedding_generator: MockEmbeddingGenerator,
) -> None:
    """
    Tests the full re-indexing flow:
    1. Ingest a URL with a "buggy" scraper that returns incorrect content.
    2. Verify the incorrect content is indexed.
    3. "Fix" the scraper to return correct content.
    4. Call the re-index API endpoint.
    5. Verify the old content is gone and the new, correct content is indexed.
    """
    # --- PHASE 1: Initial (Failed) Ingestion ---
    logger.info("\n--- Running Re-indexing E2E Test: Phase 1 (Buggy Ingestion) ---")

    # Arrange: Buggy Scraper and initial TaskWorker setup
    buggy_scraper = MockScraper(
        url_map={
            TEST_URL: ScrapeResult(
                type="success",
                final_url=TEST_URL,
                mime_type="text/html",
                content=BUGGY_CONTENT,
            )
        }
    )
    pipeline_config = {
        "processors": [
            {"type": "WebFetcher"},
            {"type": "DocumentTitleUpdater"},
            {
                "type": "TextChunker",
                "config": {
                    "embedding_type_prefix_map": {
                        "fetched_content_text": "content_chunk",
                        "fetched_content_markdown": "content_chunk",
                    }
                },
            },
            {
                "type": "EmbeddingDispatch",
                "config": {"embedding_types_to_dispatch": ["content_chunk"]},
            },
        ]
    }
    buggy_indexer = DocumentIndexer(
        pipeline_config=pipeline_config,
        llm_client=RuleBasedMockLLMClient(rules=[]),
        embedding_generator=mock_embedding_generator,
        scraper=buggy_scraper,
    )

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator,
        engine=pg_vector_db_engine,
    )
    worker.register_task_handler(
        "process_uploaded_document", buggy_indexer.process_document
    )
    worker.register_task_handler(
        "embed_and_store_batch", _helper_handle_embed_and_store_batch
    )
    worker.register_task_handler("reindex_document", handle_reindex_document)

    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(new_task_event))
    await asyncio.sleep(0.1)

    document_id = None
    try:
        # Act: Ingest the document with the buggy scraper
        source_id = f"reindex-test-{uuid.uuid4()}"
        response = await http_client.post(
            "/api/documents/upload",
            data={
                "source_type": "reindex_test",
                "source_id": source_id,
                "url": TEST_URL,
                "title": "Initial Buggy Title",
                "source_uri": TEST_URL,
            },
        )
        assert response.status_code == 202
        document_id = response.json()["document_id"]

        # Wait for the initial, buggy indexing to complete
        new_task_event.set()
        await wait_for_tasks_to_complete(
            pg_vector_db_engine, task_ids=None, timeout_seconds=20
        )

        # Assert: Verify the buggy content was indexed
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            embeddings = await db.vector.get_document_by_id(document_id)
            assert embeddings is not None
            assert len(embeddings.embeddings) > 0
            content = embeddings.embeddings[0].content
            assert content is not None and "failed" in content.lower()
            logger.info(f"Verified buggy content was indexed for doc ID {document_id}")

        # --- PHASE 2: Fix and Re-index ---
        logger.info(
            "\n--- Running Re-indexing E2E Test: Phase 2 (Fix and Re-index) ---"
        )

        # Arrange: "Fix" the scraper and update the indexer
        fixed_scraper = MockScraper(
            url_map={
                TEST_URL: ScrapeResult(
                    type="success",
                    final_url=TEST_URL,
                    mime_type="text/markdown",
                    title=CORRECT_TITLE,
                    content=CORRECT_CONTENT_MARKDOWN,
                )
            }
        )
        fixed_indexer = DocumentIndexer(
            pipeline_config=pipeline_config,
            llm_client=RuleBasedMockLLMClient(rules=[]),
            embedding_generator=mock_embedding_generator,
            scraper=fixed_scraper,
        )
        # Update the handler on the running worker
        worker.register_task_handler(
            "process_uploaded_document", fixed_indexer.process_document
        )
        # Register the re-index handler
        worker.register_task_handler("reindex_document", handle_reindex_document)

        # Act: Call the re-index API endpoint
        reindex_response = await http_client.post(
            f"/api/documents/{document_id}/reindex"
        )
        assert reindex_response.status_code == 202

        # Wait for the re-indexing task to complete
        new_task_event.set()
        await wait_for_tasks_to_complete(
            pg_vector_db_engine, task_ids=None, timeout_seconds=20
        )

        # Assert: Verify the correct content is now indexed
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            doc_record = await db.vector.get_document_by_id(document_id)
            assert doc_record is not None
            assert doc_record.title == CORRECT_TITLE
            assert len(doc_record.embeddings) > 0

            # Check that the new, correct content is there
            found_correct_chunk = any(
                EXPECTED_CHUNK in (emb.content or "") for emb in doc_record.embeddings
            )
            assert found_correct_chunk, (
                "Correct content chunk not found after re-indexing"
            )

            # Check that the old, buggy content is gone
            found_buggy_chunk = any(
                "failed" in (emb.content or "").lower() for emb in doc_record.embeddings
            )
            assert not found_buggy_chunk, (
                "Buggy content chunk still exists after re-indexing"
            )

        logger.info(
            f"Verified correct content was indexed for doc ID {document_id} after re-indexing."
        )

    finally:
        # Cleanup
        shutdown_event.set()
        worker.shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        if document_id:
            async with DatabaseContext(engine=pg_vector_db_engine) as db:
                await db.vector.delete_document(document_id)
