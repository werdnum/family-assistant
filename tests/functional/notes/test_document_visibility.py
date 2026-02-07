"""Tests for document-level visibility label filtering.

Verifies that visibility_labels propagated to the documents table are respected
by vector search queries and get_full_document_content_tool.
"""

import json
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import add_document, add_embedding
from family_assistant.storage.vector_search import VectorSearchQuery, query_vector_store
from family_assistant.tools.documents import get_full_document_content_tool
from family_assistant.tools.types import ToolExecutionContext

TEST_EMBEDDING_DIMENSION = 1536


class MockDoc:
    """Minimal Document protocol implementation for tests."""

    def __init__(
        self,
        source_type: str,
        source_id: str,
        title: str,
        visibility_labels: list[str] | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._source_type = source_type
        self._source_id = source_id
        self._title = title
        self._visibility_labels = visibility_labels
        self._metadata = metadata

    @property
    def id(self) -> int | None:
        return None

    @property
    def source_type(self) -> str:
        return self._source_type

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_uri(self) -> str | None:
        return None

    @property
    def title(self) -> str | None:
        return self._title

    @property
    def created_at(self) -> datetime | None:
        return datetime.now(UTC)

    @property
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def metadata(self) -> dict[str, Any] | None:
        return self._metadata

    @property
    def file_path(self) -> str | None:
        return None

    @property
    def visibility_labels(self) -> list[str] | None:
        return self._visibility_labels


@pytest.fixture
def embedder() -> MockEmbeddingGenerator:
    return MockEmbeddingGenerator(
        model_name="mock-embedding-model",
        dimensions=TEST_EMBEDDING_DIMENSION,
        embedding_map={
            "public": np.random
            .default_rng(1)
            .random(TEST_EMBEDDING_DIMENSION)
            .tolist(),
            "sensitive": np.random
            .default_rng(2)
            .random(TEST_EMBEDDING_DIMENSION)
            .tolist(),
            "private": np.random
            .default_rng(3)
            .random(TEST_EMBEDDING_DIMENSION)
            .tolist(),
            "query": np.random.default_rng(4).random(TEST_EMBEDDING_DIMENSION).tolist(),
        },
    )


async def _add_doc_with_embedding(
    db: DatabaseContext,
    doc: MockDoc,
    embedder: MockEmbeddingGenerator,
    embed_key: str,
    content: str,
) -> int:
    """Helper to add a document and its embedding."""
    doc_id = await add_document(db, doc)
    await add_embedding(
        db,
        document_id=doc_id,
        chunk_index=0,
        embedding_type="content_chunk",
        embedding=embedder.embedding_map[embed_key],
        embedding_model=embedder.model_name,
        content=content,
    )
    return doc_id


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_search_filters_by_visibility_grants(
    pg_vector_db_engine: AsyncEngine,
    embedder: MockEmbeddingGenerator,
) -> None:
    """Search with visibility_grants excludes documents with insufficient labels."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        public_id = await _add_doc_with_embedding(
            db,
            MockDoc("note", "pub_1", "Public Note", visibility_labels=[]),
            embedder,
            "public",
            "Public content visible to all",
        )
        sensitive_id = await _add_doc_with_embedding(
            db,
            MockDoc(
                "note", "sens_1", "Sensitive Note", visibility_labels=["sensitive"]
            ),
            embedder,
            "sensitive",
            "Sensitive information",
        )

        query_emb = embedder.embedding_map["query"]

        # With "sensitive" grant: should see both
        results = await query_vector_store(
            db,
            VectorSearchQuery(
                search_type="hybrid",
                semantic_query="content",
                keywords="content",
                embedding_model=embedder.model_name,
                visibility_grants={"sensitive"},
            ),
            query_embedding=query_emb,
        )
        doc_ids = {r["document_id"] for r in results}
        assert public_id in doc_ids
        assert sensitive_id in doc_ids

        # With empty grants: should only see public
        results = await query_vector_store(
            db,
            VectorSearchQuery(
                search_type="hybrid",
                semantic_query="content",
                keywords="content",
                embedding_model=embedder.model_name,
                visibility_grants=set(),
            ),
            query_embedding=query_emb,
        )
        doc_ids = {r["document_id"] for r in results}
        assert public_id in doc_ids
        assert sensitive_id not in doc_ids


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_search_no_grants_returns_all(
    pg_vector_db_engine: AsyncEngine,
    embedder: MockEmbeddingGenerator,
) -> None:
    """Search with visibility_grants=None returns all documents (no filtering)."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        public_id = await _add_doc_with_embedding(
            db,
            MockDoc("note", "pub_2", "Public Note 2", visibility_labels=[]),
            embedder,
            "public",
            "Public content",
        )
        sensitive_id = await _add_doc_with_embedding(
            db,
            MockDoc(
                "note", "sens_2", "Sensitive Note 2", visibility_labels=["sensitive"]
            ),
            embedder,
            "sensitive",
            "Sensitive content",
        )

        query_emb = embedder.embedding_map["query"]

        results = await query_vector_store(
            db,
            VectorSearchQuery(
                search_type="hybrid",
                semantic_query="content",
                keywords="content",
                embedding_model=embedder.model_name,
                visibility_grants=None,
            ),
            query_embedding=query_emb,
        )
        doc_ids = {r["document_id"] for r in results}
        assert public_id in doc_ids
        assert sensitive_id in doc_ids


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_search_subset_semantics(
    pg_vector_db_engine: AsyncEngine,
    embedder: MockEmbeddingGenerator,
) -> None:
    """Document with multiple labels requires ALL labels in grants."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        multi_id = await _add_doc_with_embedding(
            db,
            MockDoc(
                "note",
                "multi_1",
                "Multi Label",
                visibility_labels=["sensitive", "private"],
            ),
            embedder,
            "private",
            "Multi-label content",
        )

        query_emb = embedder.embedding_map["query"]

        # One grant is not enough
        results = await query_vector_store(
            db,
            VectorSearchQuery(
                search_type="hybrid",
                semantic_query="content",
                keywords="content",
                embedding_model=embedder.model_name,
                visibility_grants={"sensitive"},
            ),
            query_embedding=query_emb,
        )
        doc_ids = {r["document_id"] for r in results}
        assert multi_id not in doc_ids

        # Both grants needed
        results = await query_vector_store(
            db,
            VectorSearchQuery(
                search_type="hybrid",
                semantic_query="content",
                keywords="content",
                embedding_model=embedder.model_name,
                visibility_grants={"sensitive", "private"},
            ),
            query_embedding=query_emb,
        )
        doc_ids = {r["document_id"] for r in results}
        assert multi_id in doc_ids


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_get_full_document_content_respects_visibility(
    pg_vector_db_engine: AsyncEngine,
    embedder: MockEmbeddingGenerator,
) -> None:
    """get_full_document_content_tool returns not-found for inaccessible documents."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        doc_id = await _add_doc_with_embedding(
            db,
            MockDoc("note", "secret_1", "Secret Doc", visibility_labels=["top-secret"]),
            embedder,
            "sensitive",
            "Top secret content",
        )

    # With insufficient grants
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test",
            user_name="tester",
            turn_id=None,
            db_context=db,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            visibility_grants={"default"},
        )
        result = await get_full_document_content_tool(ctx, doc_id)
        assert isinstance(result, str)
        assert "not found" in result

    # With sufficient grants
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test",
            user_name="tester",
            turn_id=None,
            db_context=db,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            visibility_grants={"top-secret"},
        )
        result = await get_full_document_content_tool(ctx, doc_id)
        assert isinstance(result, str)
        assert "not found" not in result


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_get_full_document_content_no_grants_allows_all(
    pg_vector_db_engine: AsyncEngine,
    embedder: MockEmbeddingGenerator,
) -> None:
    """get_full_document_content_tool with visibility_grants=None skips filtering."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        doc_id = await _add_doc_with_embedding(
            db,
            MockDoc("note", "labeled_1", "Labeled Doc", visibility_labels=["admin"]),
            embedder,
            "sensitive",
            "Admin-only content",
        )

    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test",
            user_name="tester",
            turn_id=None,
            db_context=db,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            visibility_grants=None,
        )
        result = await get_full_document_content_tool(ctx, doc_id)
        assert isinstance(result, str)
        assert "not found" not in result


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_document_stores_visibility_labels(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Verify visibility_labels are stored in the documents table."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        doc = MockDoc("note", "vis_test_1", "Vis Test", visibility_labels=["a", "b"])
        doc_id = await add_document(db, doc)

        row = await db.fetch_one(
            text("SELECT visibility_labels FROM documents WHERE id = :id"),
            {"id": doc_id},
        )
        assert row is not None
        labels = json.loads(row["visibility_labels"])
        assert sorted(labels) == ["a", "b"]

    # Non-note document gets empty labels
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        doc2 = MockDoc("email", "email_test_1", "Email Doc", visibility_labels=None)
        doc2_id = await add_document(db, doc2)

        row2 = await db.fetch_one(
            text("SELECT visibility_labels FROM documents WHERE id = :id"),
            {"id": doc2_id},
        )
        assert row2 is not None
        labels2 = json.loads(row2["visibility_labels"])
        assert labels2 == []
