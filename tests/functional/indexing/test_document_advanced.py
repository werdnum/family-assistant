"""
End-to-end functional tests for advanced document indexing features:
LLM-generated summaries and URL ingestion with auto-title extraction.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest
import pytest_asyncio
import sqlalchemy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.document_indexer import DocumentIndexer
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import query_vectors
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.scraping import MockScraper, ScrapeResult
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import get_embedding_generator_dependency
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutputForClient,
)
from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
    get_last_message_text,
)


def _create_mock_processing_service() -> MagicMock:
    """Create a mock ProcessingService with required attributes."""
    mock = MagicMock()
    return mock


logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_EMBEDDING_MODEL = "mock-e2e-doc-model"
TEST_EMBEDDING_DIMENSION = 128  # Smaller dimension for mock testing

# --- Test Data for LLM Summary E2E ---
TEST_DOC_FOR_SUMMARY_FILENAME = "summary_test_doc.txt"
TEST_DOC_FOR_SUMMARY_CONTENT = "This is a test document about advanced quantum computing and its implications for future technology. It explores qubits, superposition, and entanglement, discussing potential breakthroughs in medicine and materials science."
EXPECTED_LLM_SUMMARY = "A document discussing quantum computing, qubits, superposition, entanglement, and potential impacts on medicine and materials science."
TEST_QUERY_FOR_SUMMARY = "quantum computing breakthroughs"
LLM_SUMMARY_TARGET_TYPE = "llm_generated_summary"

# --- Test Data for URL Indexing E2E ---
TEST_URL_TO_SCRAPE = "https://example.com/test-page-for-indexing"
MOCK_URL_TITLE = "Mocked Page Title"
MOCK_URL_CONTENT_MARKDOWN = f"""
# {MOCK_URL_TITLE}

This is the first paragraph of the mocked web page content. It discusses various interesting topics.

This is the second paragraph. It contains more details and specific keywords like 'synergy' and 'innovation'.
"""
# Expected chunks after TextChunker processes MOCK_URL_CONTENT_MARKDOWN
# Assuming chunk size allows these to be separate.
# Actual chunks produced by TextChunker (recursive=True, split by \n\n) from MOCK_URL_CONTENT_MARKDOWN:
# 1. "# Mocked Page Title"
# 2. "This is the first paragraph of the mocked web page content. It discusses various interesting topics."
# 3. "This is the second paragraph. It contains more details and specific keywords like 'synergy' and 'innovation'."
# Actual chunks from log with chunk_size=150, overlap=20:
EXPECTED_URL_CHUNK_0_CONTENT = "# Mocked Page Title This is the first paragraph of the mocked web page content It discusses various interesting topics. This is the second paragraph I"
EXPECTED_URL_CHUNK_1_CONTENT = "e second paragraph It contains more details and specific keywords like 'synergy' and 'innovation'."
TEST_QUERY_FOR_URL_CONTENT = (
    "synergy and innovation"  # Query targets chunk 1 (the second paragraph)
)


# --- Fixtures ---


@pytest_asyncio.fixture(scope="function")
async def mock_embedding_generator() -> MockEmbeddingGenerator:
    """Provides a function-scoped mock embedding generator instance."""
    # Create deterministic embeddings for known text parts
    title_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.1
    ).tolist()
    chunk0_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.2
    ).tolist()
    chunk1_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.3
    ).tolist()
    # Make semantic query embedding closer to chunk1 embedding
    query_semantic_embedding = (
        np.array(chunk1_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()
    # Make keyword query embedding closer to title embedding
    query_keyword_embedding = (
        np.array(title_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.02
    ).tolist()

    embedding_map = {
        "dummy_title": title_embedding,
        "dummy_chunk_0": chunk0_embedding,
        "dummy_chunk_1": chunk1_embedding,
        "dummy_query_semantic": query_semantic_embedding,
        "dummy_query_keyword": query_keyword_embedding,
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
    pg_vector_db_engine: AsyncEngine,  # Ensure DB is setup before app starts
    mock_embedding_generator: MockEmbeddingGenerator,  # Inject the mock generator
) -> AsyncGenerator[httpx.AsyncClient]:
    """
    Provides a test client for the FastAPI application, configured with
    the test database and mock embedding generator.
    """
    # Override the embedding generator dependency instead of modifying app.state
    # This is thread-safe and doesn't affect other tests running in parallel
    original_overrides = fastapi_app.dependency_overrides.copy()

    # Create a function that returns our mock embedding generator
    async def override_embedding_generator() -> MockEmbeddingGenerator:
        return mock_embedding_generator

    fastapi_app.dependency_overrides[get_embedding_generator_dependency] = (
        override_embedding_generator
    )
    logger.info(
        "Overrode embedding generator dependency for test client using dependency_overrides."
    )

    # Set the database engine in app.state for the get_db dependency
    original_database_engine = getattr(fastapi_app.state, "database_engine", None)
    fastapi_app.state.database_engine = pg_vector_db_engine
    logger.info("Set database_engine in app.state for test client.")

    # The pg_vector_db_engine fixture already patches storage.base.engine
    # so the app will use the correct test database.

    # Use ASGITransport for testing FastAPI apps with httpx >= 0.20.0
    transport = httpx.ASGITransport(app=fastapi_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    logger.info("Test HTTP client closed.")

    # Clean up: restore original dependency overrides and app state
    fastapi_app.dependency_overrides = original_overrides
    fastapi_app.state.database_engine = original_database_engine

    logger.info(
        "Cleaned up dependency overrides and app state after http_client fixture."
    )


# --- Helper Task Handler for the test ---
async def _helper_handle_embed_and_store_batch(
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    exec_context: ToolExecutionContext,
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    payload: dict[str, Any],
) -> None:
    logger.info(
        f"Test task handler 'test_handle_embed_and_store_batch' received payload: {payload}"
    )
    db_context = exec_context.db_context
    # Get embedding generator from the execution context directly
    embedding_generator = exec_context.embedding_generator
    assert embedding_generator is not None, (
        "Embedding generator not found in exec_context"
    )

    document_id = payload["document_id"]
    texts_to_embed: list[str] = payload["texts_to_embed"]
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    embedding_metadata_list: list[dict[str, Any]] = payload["embedding_metadata_list"]

    if not texts_to_embed:
        logger.warning(
            f"No texts to embed for document {document_id} in 'embed_and_store_batch'."
        )
        return

    embedding_result = await embedding_generator.generate_embeddings(texts_to_embed)

    for i, vector in enumerate(embedding_result.embeddings):
        meta = embedding_metadata_list[
            i
        ]  # This meta contains embedding_type, chunk_index, original_content_metadata
        await db_context.vector.add_embedding(
            document_id=document_id,
            chunk_index=meta.get("chunk_index", 0),  # From embedding_metadata_list
            embedding_type=meta["embedding_type"],  # From embedding_metadata_list
            embedding=vector,
            embedding_model=embedding_result.model_name,
            content=texts_to_embed[i],  # The text itself
            embedding_doc_metadata=meta.get("original_content_metadata"),  # Store this
        )
    logger.info(
        f"Stored {len(texts_to_embed)} embeddings for document {document_id} via _helper_handle_embed_and_store_batch."
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_document_indexing_with_llm_summary_e2e(
    pg_vector_db_engine: AsyncEngine,
    http_client: httpx.AsyncClient,
    mock_embedding_generator: MockEmbeddingGenerator,
) -> None:
    """
    End-to-end test for document ingestion with LLM-generated summary.
    """
    logger.info("\n--- Running Document Indexing with LLM Summary E2E Test ---")

    # --- Arrange: Mock LLM Client for Summarization ---
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def summary_matcher(actual_kwargs: dict[str, Any]) -> bool:
        # method_name argument removed as it's no longer passed or needed
        # Check if the LLM is being asked to extract a summary
        if not (
            actual_kwargs.get("tools")
            and actual_kwargs["tools"][0].get("function", {}).get("name")
            == "extract_summary"
        ):
            return False
        # Check if the input content is present in messages
        # The LLMSummaryGeneratorProcessor uses format_user_message_with_file,
        # which might put content in various parts of the message structure.
        # For a text file, it's likely in messages[1]['content'] or messages[1]['content'][0]['text']
        # We need to check the structure of the message for file processing.
        messages = actual_kwargs.get("messages", [])
        if not messages:
            return False

        last_message = messages[-1]
        if last_message.role != "user":
            return False

        content = last_message.content
        if not isinstance(content, list):  # Expecting multipart content for files
            # If it's simple text content, check if TEST_DOC_FOR_SUMMARY_CONTENT is in it
            # This path might not be hit if format_user_message_with_file always makes a list
            if isinstance(content, str):
                return TEST_DOC_FOR_SUMMARY_CONTENT in content
            return False

        has_process_file_text = False
        has_text_plain_file_placeholder = False

        for part in content:
            # Handle both dict and Pydantic model objects
            # Check for text content
            part_type = None
            if isinstance(part, dict):
                part_type = part.get("type")
            elif hasattr(part, "type"):
                part_type = part.type

            if part_type == "text":
                # Check if it's a dict or a Pydantic model object
                part_text = None
                if isinstance(part, dict):
                    part_text = part.get("text")
                elif hasattr(part, "text"):
                    part_text = part.text
                if part_text == "Process the provided file.":
                    has_process_file_text = True
            # Check for the file placeholder that format_user_message_with_file creates
            elif part_type == "file_placeholder":
                # Extract file_reference from dict or Pydantic model
                file_ref = None
                if isinstance(part, dict):
                    file_ref = part.get("file_reference", {})
                elif hasattr(part, "file_reference"):
                    file_ref = part.file_reference
                else:
                    file_ref = {}

                # In this test, the uploaded file is text/plain.
                # We can't check the content of file_ref.get("file_path") here easily,
                # but if we see this structure, we assume it's our test file.
                if isinstance(file_ref, dict):
                    mime_type = file_ref.get("mime_type")
                else:
                    mime_type = getattr(file_ref, "mime_type", None)
                if mime_type == "text/plain":
                    has_text_plain_file_placeholder = True

        # The matcher should return true if it's a text prompt containing the summary content OR
        # if it's a file processing prompt for a text file.
        return (has_process_file_text and has_text_plain_file_placeholder) or (
            TEST_DOC_FOR_SUMMARY_CONTENT
            in get_last_message_text(actual_kwargs["messages"])
            and not has_text_plain_file_placeholder
        )

    mock_llm_output = MockLLMOutputForClient(
        content=None,
        tool_calls=[
            ToolCallItem(
                id="call_summary_123",
                type="function",
                function=ToolCallFunction(
                    name="extract_summary",
                    arguments=json.dumps({"summary": EXPECTED_LLM_SUMMARY}),
                ),
            )
        ],
    )
    mock_llm_client = RuleBasedMockLLMClient(rules=[(summary_matcher, mock_llm_output)])

    # --- Arrange: Update Mock Embeddings ---
    summary_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.4
    ).tolist()
    query_summary_embedding = (
        np.array(summary_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()

    # Define URL chunk embeddings first
    url_chunk_0_embedding_val = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.5
    ).tolist()
    url_chunk_1_embedding_val = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.6
    ).tolist()

    # Define query for URL content embedding using the pre-defined chunk embedding
    query_for_url_content_embedding_val = (
        np.array(url_chunk_1_embedding_val)  # Use the variable here
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()

    mock_embedding_generator.embedding_map.update(  # type: ignore
        {
            # The LLMSummaryProcessor outputs the JSON string of the extracted data
            json.dumps({"summary": EXPECTED_LLM_SUMMARY}, indent=2): summary_embedding,
            TEST_QUERY_FOR_SUMMARY: query_summary_embedding,
            # Add mappings for URL content
            EXPECTED_URL_CHUNK_0_CONTENT: url_chunk_0_embedding_val,
            EXPECTED_URL_CHUNK_1_CONTENT: url_chunk_1_embedding_val,
            TEST_QUERY_FOR_URL_CONTENT: query_for_url_content_embedding_val,
        }
    )
    # query_summary_embedding and query_for_url_content_embedding_val are local variables.
    # No need to store them on mock_embedding_generator instance.

    # --- Arrange: Define Pipeline Config for LLM Summary Test ---
    test_pipeline_config_summary = {
        "processors": [
            {
                "type": "LLMSummaryGenerator",
                "config": {
                    "input_content_types": ["original_document_file"],
                    "target_embedding_type": LLM_SUMMARY_TARGET_TYPE,
                },
            },
            {
                "type": "EmbeddingDispatch",
                "config": {
                    "embedding_types_to_dispatch": [
                        "title",
                        "content_chunk",
                        LLM_SUMMARY_TARGET_TYPE,
                    ]
                },
            },
        ]
    }
    # No Scraper needed for this file-based summary test
    mock_scraper_dummy = MockScraper(url_map={})

    document_indexer = DocumentIndexer(
        pipeline_config=test_pipeline_config_summary,
        llm_client=mock_llm_client,  # type: ignore
        embedding_generator=mock_embedding_generator,
        scraper=mock_scraper_dummy,
    )

    # --- Arrange: Task Worker Setup ---
    # The mock embedding generator is injected via dependency override in http_client fixture
    # The mock LLM client is passed directly to DocumentIndexer, no need to set app.state

    mock_chat_interface_summary = MagicMock()

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface_summary,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator,
        engine=pg_vector_db_engine,  # Pass the database engine
    )
    worker.register_task_handler(
        "process_uploaded_document", document_indexer.process_document
    )
    worker.register_task_handler(
        "embed_and_store_batch", _helper_handle_embed_and_store_batch
    )

    worker_id = f"test-doc-summary-worker-{uuid.uuid4()}"
    logger.info(f"Starting document summary worker: {worker_id}")  # Use worker_id
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    await asyncio.sleep(0.1)

    document_db_id = None
    indexing_task_id = None
    try:
        # --- Act: Call API to Ingest Document (as a file upload) ---
        doc_source_id_summary = f"test-doc-summary-{uuid.uuid4()}"
        api_files_data = {
            "uploaded_file": (
                TEST_DOC_FOR_SUMMARY_FILENAME,
                TEST_DOC_FOR_SUMMARY_CONTENT.encode(
                    "utf-8"
                ),  # Provide raw bytes directly
                "text/plain",
            )
        }
        api_form_data_summary = {
            "source_type": "summary_test_upload",
            "source_id": doc_source_id_summary,
            "title": "Document for LLM Summary Test",
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": json.dumps({"test_type": "llm_summary"}),
            "source_uri": "",  # Add missing source_uri
            # No content_parts, we are testing file processing for summary
        }
        logger.info(
            f"Calling POST /api/documents/upload for LLM summary test, source_id: {doc_source_id_summary}"
        )
        response = await http_client.post(
            "/api/documents/upload", data=api_form_data_summary, files=api_files_data
        )

        assert response.status_code == 202, (
            f"API call failed: {response.status_code} - {response.text}"
        )
        response_data = response.json()
        document_db_id = response_data["document_id"]

        # Fetch task ID
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)
            select_task_stmt = (
                select(tasks_table.c.task_id)
                .where(
                    tasks_table.c.payload.cast(sqlalchemy.Text).like(
                        f'%"document_id": {document_db_id}%'
                    )
                )
                .order_by(tasks_table.c.created_at.desc())
                .limit(1)
            )
            task_info = await db.fetch_one(select_task_stmt)
            assert task_info is not None, (
                f"Could not find task for doc ID {document_db_id}"
            )
            indexing_task_id = task_info["task_id"]

        # Wait for indexing
        test_new_task_event.set()
        await wait_for_tasks_to_complete(
            pg_vector_db_engine, task_ids={indexing_task_id}, timeout_seconds=20.0
        )

        # --- Assert: Query for the LLM-generated summary ---
        summary_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(
                f"Querying vectors for LLM summary using text: '{TEST_QUERY_FOR_SUMMARY}'"
            )
            summary_query_results = await query_vectors(
                db,
                query_embedding=query_summary_embedding,  # Use local variable
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"source_id": doc_source_id_summary},
                embedding_type_filter=[LLM_SUMMARY_TARGET_TYPE],
            )

        assert summary_query_results is not None, (
            "LLM summary query_vectors returned None"
        )
        assert len(summary_query_results) > 0, (
            "No results returned from LLM summary vector query"
        )

        found_summary_result = summary_query_results[0]
        logger.info(f"Found LLM summary result: {found_summary_result}")

        assert found_summary_result.get("source_id") == doc_source_id_summary
        assert found_summary_result.get("embedding_type") == LLM_SUMMARY_TARGET_TYPE
        # The content stored is the JSON string of the tool call arguments
        expected_stored_summary_content = json.dumps(
            {"summary": EXPECTED_LLM_SUMMARY}, indent=2
        )
        assert (
            found_summary_result.get("embedding_source_content")
            == expected_stored_summary_content
        )
        assert float(found_summary_result.get("distance", 1.0)) < 0.1, (
            "Distance for LLM summary should be small"
        )

        # LLM call verification removed as per user request.
        # The successful creation of the summary embedding, verified above,
        # implies the LLM was called correctly with the mock setup.

        logger.info("--- Document Indexing with LLM Summary E2E Test Passed ---")

    finally:
        # Cleanup
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.vector.delete_document(document_db_id)
            except Exception as e:
                logger.warning(f"Cleanup error for document {document_db_id}: {e}")
        if indexing_task_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    delete_stmt = tasks_table.delete().where(
                        tasks_table.c.task_id == indexing_task_id
                    )
                    await db_cleanup.execute_with_retry(delete_stmt)
            except Exception as e:
                logger.warning(f"Cleanup error for task {indexing_task_id}: {e}")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_url_indexing_e2e(
    pg_vector_db_engine: AsyncEngine,
    http_client: httpx.AsyncClient,
    mock_embedding_generator: MockEmbeddingGenerator,
) -> None:
    """
    End-to-end test for URL ingestion via API, fetching with MockScraper,
    indexing via task worker, and vector query retrieval.
    This test explicitly PROVIDES a title during ingestion.
    """
    logger.info("\n--- Running URL Indexing E2E Test (Manual Title) via API ---")

    # --- Arrange: MockScraper ---
    # Instantiate ScrapeResult based on lint error feedback
    mock_scrape_result = ScrapeResult(
        type="success",  # Required argument
        final_url=TEST_URL_TO_SCRAPE,  # Required argument
        mime_type="text/markdown",  # Accepted keyword argument
        title=MOCK_URL_TITLE,  # Pass as argument if ScrapeResult supports it
        content=MOCK_URL_CONTENT_MARKDOWN,  # Pass as argument
    )

    # Instantiate MockScraper with url_map
    mock_scraper = MockScraper(url_map={TEST_URL_TO_SCRAPE: mock_scrape_result})
    logger.info(f"MockScraper configured for URL: {TEST_URL_TO_SCRAPE}")

    # Instantiate MockScraper with url_map
    mock_scraper = MockScraper(url_map={TEST_URL_TO_SCRAPE: mock_scrape_result})
    logger.info(f"MockScraper configured for URL: {TEST_URL_TO_SCRAPE}")

    # --- Arrange: Update Mock Embeddings for URL content ---
    # Ensure the mock_embedding_generator used in this test has the necessary embeddings

    # Embedding for the first paragraph (now EXPECTED_URL_CHUNK_0_CONTENT)
    url_para1_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.7
    ).tolist()
    # Embedding for the second paragraph (now EXPECTED_URL_CHUNK_1_CONTENT)
    url_para2_embedding = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.8
    ).tolist()

    mock_embedding_generator.embedding_map[EXPECTED_URL_CHUNK_0_CONTENT] = (
        url_para1_embedding
    )
    mock_embedding_generator.embedding_map[EXPECTED_URL_CHUNK_1_CONTENT] = (
        url_para2_embedding
    )

    # Query embedding is made close to the embedding of the second paragraph (EXPECTED_URL_CHUNK_1_CONTENT)
    query_url_content_embedding = (
        np.array(url_para2_embedding)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()
    mock_embedding_generator.embedding_map[TEST_QUERY_FOR_URL_CONTENT] = (
        query_url_content_embedding
    )
    # query_url_content_embedding is a local variable.
    # No need to store it on mock_embedding_generator instance.
    logger.info(
        "Updated mock_embedding_generator.embedding_map with URL-specific embeddings for test_url_indexing_e2e."
    )

    # --- Arrange: Instantiate Pipeline and Indexer for URL processing ---
    # Define Pipeline Config for URL Indexing Test
    test_pipeline_config_url = {
        "processors": [
            {"type": "WebFetcher", "config": {}},  # Scraper injected by DocumentIndexer
            {
                "type": "TextChunker",
                "config": {
                    "chunk_size": 150,
                    "chunk_overlap": 20,
                    "embedding_type_prefix_map": {
                        "fetched_content_markdown": "content_chunk"
                    },
                },
            },
            {
                "type": "EmbeddingDispatch",
                "config": {"embedding_types_to_dispatch": ["content_chunk"]},
            },
        ]
    }
    # No LLM needed for this URL indexing test's pipeline
    mock_llm_client_dummy = RuleBasedMockLLMClient(rules=[])

    document_indexer_for_url = DocumentIndexer(
        pipeline_config=test_pipeline_config_url,
        llm_client=mock_llm_client_dummy,  # type: ignore
        embedding_generator=mock_embedding_generator,
        scraper=mock_scraper,  # Pass the mock_scraper for WebFetcher
    )
    logger.info(
        "DocumentIndexer for URL (manual title) initialized with specific pipeline config."
    )

    # --- Arrange: Task Worker Setup ---
    # The mock embedding generator is injected via dependency override in http_client fixture
    mock_chat_interface_url = MagicMock()

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface_url,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator,
        engine=pg_vector_db_engine,  # Pass the database engine
    )
    worker.register_task_handler(
        "process_uploaded_document",
        document_indexer_for_url.process_document,  # Use the URL-specific indexer
    )
    worker.register_task_handler(
        "embed_and_store_batch",
        _helper_handle_embed_and_store_batch,
    )

    worker_id = f"test-url-worker-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    logger.info(f"Started background task worker {worker_id} for URL test...")
    await asyncio.sleep(0.1)

    document_db_id = None
    indexing_task_id = None
    try:
        # --- Act: Call API to Ingest URL ---
        url_doc_source_id = f"test-url-doc-{uuid.uuid4()}"
        api_form_data_url = {
            "source_type": "url_test_upload",
            "source_id": url_doc_source_id,
            "title": (
                "Test URL Ingestion: " + MOCK_URL_TITLE
            ),  # Title for the document record
            "url": TEST_URL_TO_SCRAPE,  # Provide the URL to be scraped
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": json.dumps({"test_type": "url_indexing"}),
            "source_uri": TEST_URL_TO_SCRAPE,  # Canonical URI is the URL itself
        }
        logger.info(f"Calling POST /api/documents/upload for URL: {TEST_URL_TO_SCRAPE}")
        response = await http_client.post(
            "/api/documents/upload", data=api_form_data_url
        )

        assert response.status_code == 202, (
            f"API call for URL failed: {response.status_code} - {response.text}"
        )
        response_data = response.json()
        document_db_id = response_data["document_id"]
        logger.info(f"API call for URL successful. Document DB ID: {document_db_id}")

        # Fetch task ID
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)  # Give task time to appear
            select_task_stmt = (
                select(tasks_table.c.task_id)
                .where(
                    tasks_table.c.payload.cast(sqlalchemy.Text).like(
                        f'%"document_id": {document_db_id}%'
                    )
                )
                .order_by(tasks_table.c.created_at.desc())
                .limit(1)
            )
            task_info = await db.fetch_one(select_task_stmt)
            assert task_info is not None, (
                f"Could not find enqueued task for URL document ID {document_db_id}"
            )
            indexing_task_id = task_info["task_id"]
            logger.info(
                f"Found indexing task ID: {indexing_task_id} for URL document DB ID: {document_db_id}"
            )

        # Wait for indexing task and subsequent embedding tasks to complete
        test_new_task_event.set()
        logger.info(
            f"Waiting for task {indexing_task_id} (and children) to complete..."
        )
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            task_ids=None,  # Wait for ALL tasks to complete, including spawned ones
            timeout_seconds=25.0,
        )
        logger.info(
            f"All tasks related to {indexing_task_id} (and children) reported as complete."
        )

        # --- Assert: Query for the fetched URL content ---
        url_content_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            logger.info(
                f"Querying vectors for URL content using text: '{TEST_QUERY_FOR_URL_CONTENT}'"
            )
            url_content_query_results = await query_vectors(
                db,
                query_embedding=query_url_content_embedding,  # Use local variable
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={
                    "source_id": url_doc_source_id
                },  # Filter by the document's source_id
                embedding_type_filter=[
                    "content_chunk"
                ],  # Expecting chunks from TextChunker
            )

        assert url_content_query_results is not None, (
            "URL content query_vectors returned None"
        )
        assert len(url_content_query_results) > 0, (
            "No results returned from URL content vector query"
        )
        logger.info(
            f"URL content query returned {len(url_content_query_results)} result(s)."
        )

        # We expect two chunks from MOCK_URL_CONTENT_MARKDOWN
        # Check if both expected chunks are present in the results for this document_id
        found_chunk_0 = False
        found_chunk_1 = False
        for result in url_content_query_results:
            if result.get("source_id") != url_doc_source_id:
                continue  # Should be filtered by query, but double check

            assert result.get("embedding_type") == "content_chunk"
            assert "distance" in result
            # Check metadata from WebFetcherProcessor
            embedding_meta = result.get("embedding_metadata", {})
            assert (
                embedding_meta.get("original_url") == TEST_URL_TO_SCRAPE
            )  # Corrected key
            assert (
                embedding_meta.get("mime_type") == "text/markdown"
            )  # From WebFetcher output
            # Title might be in embedding_doc_meta if WebFetcher adds it, or in main doc title
            # For now, WebFetcherProcessor doesn't explicitly add title to chunk metadata.

            if result.get("embedding_source_content") == EXPECTED_URL_CHUNK_0_CONTENT:
                found_chunk_0 = True
                logger.info(f"Found expected URL chunk 0: {result}")
                # No strict distance check for chunk 0 with this specific query,
                # as the query is tailored for chunk 1.
            elif result.get("embedding_source_content") == EXPECTED_URL_CHUNK_1_CONTENT:
                found_chunk_1 = True
                logger.info(f"Found expected URL chunk 1: {result}")
                # Apply strict distance check only for the chunk targeted by the query
                assert float(result["distance"]) < 0.1, (
                    f"Distance for targeted chunk 1 ({result['distance']}) was not < 0.1. "
                    f"Query: '{TEST_QUERY_FOR_URL_CONTENT}', Chunk content: '{EXPECTED_URL_CHUNK_1_CONTENT}'"
                )

        assert found_chunk_0, (
            f"Expected URL chunk 0 not found in query results. Content: {EXPECTED_URL_CHUNK_0_CONTENT}"
        )
        assert found_chunk_1, (
            f"Expected URL chunk 1 not found in query results. Content: {EXPECTED_URL_CHUNK_1_CONTENT}"
        )

        logger.info("--- URL Indexing E2E Test via API Passed ---")

    finally:
        # Cleanup
        logger.info(f"Stopping background task worker {worker_id} for URL test...")
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
            logger.info(f"Background task worker {worker_id} for URL test stopped.")
        except TimeoutError:
            logger.warning(f"Timeout stopping worker task {worker_id}. Cancelling.")
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        except Exception as e:
            logger.error(f"Error stopping worker task {worker_id}: {e}", exc_info=True)

        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.vector.delete_document(document_db_id)
                    logger.info(f"Cleaned up test URL document DB ID {document_db_id}")
            except Exception as cleanup_err:
                logger.warning(f"Error during test URL document cleanup: {cleanup_err}")
        if indexing_task_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    delete_stmt = tasks_table.delete().where(
                        tasks_table.c.task_id == indexing_task_id
                    )
                    await db_cleanup.execute_with_retry(delete_stmt)
                    logger.info(f"Cleaned up test URL task ID {indexing_task_id}")
            except Exception as cleanup_err:
                logger.warning(f"Error during test URL task cleanup: {cleanup_err}")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_url_indexing_auto_title_e2e(
    pg_vector_db_engine: AsyncEngine,
    http_client: httpx.AsyncClient,
    mock_embedding_generator: MockEmbeddingGenerator,
) -> None:
    """
    End-to-end test for URL ingestion where the title is NOT provided,
    expecting it to be automatically extracted and updated by DocumentTitleUpdaterProcessor.
    """
    logger.info("\n--- Running URL Indexing E2E Test (Auto Title) via API ---")

    # --- Arrange: MockScraper ---
    mock_scrape_result = ScrapeResult(
        type="success",
        final_url=TEST_URL_TO_SCRAPE,
        mime_type="text/markdown",
        title=MOCK_URL_TITLE,  # This title should be auto-extracted
        content=MOCK_URL_CONTENT_MARKDOWN,
    )

    mock_scraper = MockScraper(url_map={TEST_URL_TO_SCRAPE: mock_scrape_result})
    logger.info(f"MockScraper configured for URL: {TEST_URL_TO_SCRAPE}")

    # Set the mock scraper on app state so API calls can use it
    original_scraper = getattr(fastapi_app.state, "scraper", None)
    fastapi_app.state.scraper = mock_scraper

    # --- Arrange: Update Mock Embeddings for URL content ---
    url_chunk_0_embedding_val = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32)
        * 0.75  # Slightly different from other test
    ).tolist()
    url_chunk_1_embedding_val = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.85
    ).tolist()
    mock_embedding_generator.embedding_map[EXPECTED_URL_CHUNK_0_CONTENT] = (
        url_chunk_0_embedding_val
    )
    mock_embedding_generator.embedding_map[EXPECTED_URL_CHUNK_1_CONTENT] = (
        url_chunk_1_embedding_val
    )
    query_url_content_embedding = (
        np.array(url_chunk_1_embedding_val)
        + np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32) * 0.01
    ).tolist()
    mock_embedding_generator.embedding_map[TEST_QUERY_FOR_URL_CONTENT] = (
        query_url_content_embedding
    )
    # query_url_content_embedding is a local variable.
    # No need to store it on mock_embedding_generator instance.
    logger.info(
        "Updated mock_embedding_generator.embedding_map for URL auto-title test."
    )

    # --- Arrange: Instantiate Pipeline and Indexer for URL auto-title processing ---
    test_pipeline_config_auto_title = {
        "processors": [
            {"type": "WebFetcher", "config": {}},
            {"type": "DocumentTitleUpdater", "config": {}},  # Key addition
            {
                "type": "TextChunker",
                "config": {
                    "chunk_size": 150,
                    "chunk_overlap": 20,
                    "embedding_type_prefix_map": {
                        "fetched_content_markdown": "content_chunk"
                    },
                },
            },
            {
                "type": "EmbeddingDispatch",
                "config": {"embedding_types_to_dispatch": ["content_chunk"]},
            },
        ]
    }
    mock_llm_client_dummy = RuleBasedMockLLMClient(rules=[])
    document_indexer_auto_title = DocumentIndexer(
        pipeline_config=test_pipeline_config_auto_title,
        llm_client=mock_llm_client_dummy,  # type: ignore
        embedding_generator=mock_embedding_generator,
        scraper=mock_scraper,
    )
    logger.info("DocumentIndexer for URL (auto title) initialized.")

    # --- Arrange: Task Worker Setup ---
    mock_chat_interface_auto_title = MagicMock()

    worker = TaskWorker(
        processing_service=_create_mock_processing_service(),
        chat_interface=mock_chat_interface_auto_title,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_generator,
        engine=pg_vector_db_engine,  # Pass the database engine
    )
    worker.register_task_handler(
        "process_uploaded_document",
        document_indexer_auto_title.process_document,
    )
    worker.register_task_handler(
        "embed_and_store_batch",
        _helper_handle_embed_and_store_batch,
    )

    worker_id = f"test-auto-title-worker-{uuid.uuid4()}"
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(test_new_task_event))
    logger.info(f"Started background task worker {worker_id} for auto-title test...")
    await asyncio.sleep(0.1)

    document_db_id = None
    indexing_task_id = None
    try:
        # --- Act: Call API to Ingest URL (placeholder title that will be replaced) ---
        url_doc_source_id = f"test-auto-title-doc-{uuid.uuid4()}"
        api_form_data_url_no_title = {
            "source_type": "url_auto_title_test",
            "source_id": url_doc_source_id,
            "title": "Placeholder - Should be replaced by auto-extracted title",
            "url": TEST_URL_TO_SCRAPE,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": json.dumps({"test_type": "url_auto_title_indexing"}),
            "source_uri": TEST_URL_TO_SCRAPE,
        }
        logger.info(
            f"Calling POST /api/documents/upload for URL (placeholder title): {TEST_URL_TO_SCRAPE}"
        )
        response = await http_client.post(
            "/api/documents/upload", data=api_form_data_url_no_title
        )

        assert response.status_code == 202, (
            f"API call for URL (no title) failed: {response.status_code} - {response.text}"
        )
        response_data = response.json()
        document_db_id = response_data["document_id"]
        logger.info(
            f"API call for URL (no title) successful. Document DB ID: {document_db_id}"
        )

        # Fetch task ID
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            await asyncio.sleep(0.2)
            select_task_stmt = (
                select(tasks_table.c.task_id)
                .where(
                    tasks_table.c.payload.cast(sqlalchemy.Text).like(
                        f'%"document_id": {document_db_id}%'
                    )
                )
                .order_by(tasks_table.c.created_at.desc())
                .limit(1)
            )
            task_info = await db.fetch_one(select_task_stmt)
            assert task_info is not None, (
                f"Could not find task for auto-title doc ID {document_db_id}"
            )
            indexing_task_id = task_info["task_id"]

        # Wait for indexing
        test_new_task_event.set()
        await wait_for_tasks_to_complete(
            pg_vector_db_engine,
            task_ids=None,  # Wait for all, including spawned embedding tasks
            timeout_seconds=60.0,  # Increased timeout for URL indexing with title update
        )

        # --- Assert: Verify Document Title in DB ---
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            doc_record = await db.vector.get_document_by_id(document_db_id)
            assert doc_record is not None, (
                f"Document record {document_db_id} not found in DB."
            )
            assert doc_record.title == MOCK_URL_TITLE, (
                f"Document title was not updated. Expected '{MOCK_URL_TITLE}', got '{doc_record.title}'"
            )
            logger.info(f"Verified document title in DB: '{doc_record.title}'")

        # --- Assert: Query for the fetched URL content ---
        url_content_query_results = None
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            url_content_query_results = await query_vectors(
                db,
                query_embedding=query_url_content_embedding,  # Use local variable
                embedding_model=TEST_EMBEDDING_MODEL,
                limit=5,
                filters={"source_id": url_doc_source_id},
                embedding_type_filter=["content_chunk"],
            )

        assert url_content_query_results is not None
        assert len(url_content_query_results) > 0

        found_chunk_1 = False
        for result in url_content_query_results:
            if result.get("source_id") != url_doc_source_id:
                continue
            # Crucially, check that the title in the query result is the auto-updated one
            assert result.get("title") == MOCK_URL_TITLE, (
                f"Query result title mismatch. Expected '{MOCK_URL_TITLE}', got '{result.get('title')}'"
            )

            if result.get("embedding_source_content") == EXPECTED_URL_CHUNK_1_CONTENT:
                found_chunk_1 = True
                assert float(result["distance"]) < 0.1
                logger.info(
                    f"Found targeted URL chunk 1 with auto-updated title: {result}"
                )

        assert found_chunk_1, (
            "Expected URL chunk 1 not found in query results for auto-title test."
        )

        logger.info("--- URL Indexing E2E Test (Auto Title) via API Passed ---")

    finally:
        # Cleanup
        logger.info(
            f"Stopping background task worker {worker_id} for auto-title test..."
        )
        test_shutdown_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except TimeoutError:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        if document_db_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    await db_cleanup.vector.delete_document(document_db_id)
            except Exception as e:
                logger.warning(
                    f"Cleanup error for auto-title document {document_db_id}: {e}"
                )
        if indexing_task_id:
            try:
                async with DatabaseContext(engine=pg_vector_db_engine) as db_cleanup:
                    delete_stmt = tasks_table.delete().where(
                        tasks_table.c.task_id == indexing_task_id
                    )
                    await db_cleanup.execute_with_retry(delete_stmt)
            except Exception as e:
                logger.warning(
                    f"Cleanup error for auto-title task {indexing_task_id}: {e}"
                )

        # Restore original scraper
        if original_scraper is not None:
            fastapi_app.state.scraper = original_scraper
        elif hasattr(fastapi_app.state, "scraper"):
            # Remove scraper if it didn't exist before
            del fastapi_app.state.scraper
