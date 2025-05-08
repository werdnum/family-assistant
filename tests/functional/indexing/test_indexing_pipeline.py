"""
Functional test for the basic document indexing pipeline.
"""
import pytest
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple, Awaitable, Callable

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
from family_assistant.storage import schema # To access tables for direct query
from family_assistant.embeddings import MockEmbeddingGenerator, EmbeddingGenerator, EmbeddingResult
from family_assistant.indexing.pipeline import IndexingPipeline, IndexableContent
from family_assistant.indexing.processors.metadata_processors import TitleExtractor
from family_assistant.indexing.processors.text_processors import TextChunker
from family_assistant.indexing.processors.dispatch_processors import EmbeddingDispatchProcessor
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.tools.types import ToolExecutionContext

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


@pytest.mark.asyncio
async def test_indexing_pipeline_e2e(pg_vector_db_engine: AsyncEngine):
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

    mock_embed_generator = TestSpecificMockEmbeddingGenerator(
        embedding_map=embedding_map, # Start with empty, will populate
        model_name=TEST_EMBEDDING_MODEL_NAME,
        default_embedding=[0.0] * TEST_EMBEDDING_DIMENSION
    )

    # Mock enqueue_task to call handle_embed_and_store_batch directly
    dispatched_task_payloads: List[Dict[str, Any]] = []

    async def mock_enqueue_task(
        task_id: str, task_type: str, payload: Optional[Dict[str, Any]] = None, **kwargs
    ):
        nonlocal dispatched_task_payloads
        if task_type == "embed_and_store_batch" and payload:
            dispatched_task_payloads.append(payload)
            logger.info(f"Mock enqueue_task: Intercepted {task_type} with payload for doc_id {payload.get('document_id')}")
            # Call the handler directly
            async with get_db_context(engine=pg_vector_db_engine) as db_ctx_for_handler:
                await handle_embed_and_store_batch(
                    db_context=db_ctx_for_handler,
                    payload=payload,
                    embedding_generator=mock_embed_generator,
                )
        else:
            logger.warning(f"Mock enqueue_task: Received unhandled task_type {task_type}")

    async with get_db_context(engine=pg_vector_db_engine) as db_context:
        tool_exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-indexing-conv",
            db_context=db_context, # Use the outer context for pipeline
            calendar_config={},
            application=None,
            enqueue_task_fn=mock_enqueue_task
        )

        # Create and store the document
        test_document_protocol = MockDocumentImpl(
            source_type="test", source_id=doc_source_id, title=doc_title
        )
        doc_db_id = await add_document(db_context, test_document_protocol)
        original_doc_record = await get_document_by_source_id(db_context, doc_source_id)
        assert original_doc_record and original_doc_record.id == doc_db_id

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

        # --- Assert ---
        assert len(dispatched_task_payloads) > 0, "No embed_and_store_batch task was dispatched"

        # Verify embeddings in DB
        stmt = schema.document_embeddings.select().where(schema.document_embeddings.c.document_id == doc_db_id)
        stored_embeddings_rows = await db_context.fetch_all(stmt)

        assert len(stored_embeddings_rows) >= 2, "Expected at least title and one chunk embedding"

        title_embedding_found = False
        chunk_embeddings_found = 0
        expected_chunk_texts = [ # Based on chunker logic and test content/size
            "Apples are red. Bananas are", # Chunk 1
            "anas are yellow. Oranges are", # Chunk 2 (overlap "anas ")
            "ges are orange and tasty." # Chunk 3 (overlap "ges a")
        ]

        for row_proxy in stored_embeddings_rows:
            row = dict(row_proxy) # Convert RowProxy to dict for easier access
            assert row["embedding_model"] == TEST_EMBEDDING_MODEL_NAME
            if row["embedding_type"] == "title":
                assert row["content"] == doc_title
                title_embedding_found = True
            elif row["embedding_type"] == "raw_text_chunk":
                assert row["content"] in expected_chunk_texts
                chunk_embeddings_found += 1

        assert title_embedding_found, "Title embedding not found"
        assert chunk_embeddings_found == len(expected_chunk_texts), \
            f"Expected {len(expected_chunk_texts)} chunk embeddings, found {chunk_embeddings_found}"

        # Verify search
        query_text_for_chunk = "yellow bananas" # Should match chunk 2
        query_vector_result = await mock_embed_generator.generate_embeddings([query_text_for_chunk])
        query_embedding = query_vector_result.embeddings[0]

        search_results = await query_vectors(
            db_context, query_embedding, TEST_EMBEDDING_MODEL_NAME, limit=5
        )
        assert len(search_results) > 0, "Vector search returned no results"

        found_matching_chunk_in_search = False
        for res in search_results:
            if res["document_id"] == doc_db_id and "yellow. Oranges are" in res["embedding_source_content"]:
                found_matching_chunk_in_search = True
                break
        assert found_matching_chunk_in_search, "Relevant chunk not found via vector search"

    logger.info("Indexing pipeline E2E test passed.")
