"""
Comprehensive tests for vector search functionality.
Tests advanced search features, error conditions, and edge cases.
"""

import asyncio
import math
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.storage.database import Database
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import get_db

FIXTURE_SOURCE_IDS = [
    "business_plan",
    "edu_article",
    "finance_note",
    "health_newsletter",
    "tech_report",
]
TECHNOLOGY_EMBEDDING = [0.0, 1.0] + [0.0] * 1534


class TestDocument:
    """Test document class that implements the Document protocol."""

    def __init__(
        self,
        source_type: str,
        source_id: str,
        id: int | None = None,
        source_uri: str | None = None,
        title: str | None = None,
        created_at: datetime | None = None,
        # ast-grep-ignore: no-dict-any - mock document metadata has dynamic third-party fields
        metadata: dict[str, Any] | None = None,
        file_path: str | None = None,
    ) -> None:
        self._id = id
        self._source_type = source_type
        self._source_id = source_id
        self._source_uri = source_uri
        self._title = title
        self._created_at = created_at
        self._metadata = metadata
        self._file_path = file_path

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
        return self._file_path

    @property
    def visibility_labels(self) -> list[str] | None:
        return None


@pytest.fixture
async def comprehensive_vector_client(
    pg_vector_db_engine: AsyncEngine,
) -> AsyncGenerator[httpx.AsyncClient]:
    """API client with comprehensive test data setup."""

    async def override_get_db() -> AsyncGenerator[Database]:
        db = Database(engine=pg_vector_db_engine)
        yield db

    fastapi_app.dependency_overrides[get_db] = override_get_db

    # Create embedder with diverse test vectors using proper model/dimensions
    embedder = MockEmbeddingGenerator(
        model_name="gemini-exp-03-07",  # Use model with proper index
        dimensions=1536,  # Use correct dimensions
        embedding_map={
            "finance": [1.0] + [0.0] * 1535,
            "technology": [0.0, 1.0] + [0.0] * 1534,
            "health": [0.0, 0.0, 1.0] + [0.0] * 1533,
            "education": [0.0, 0.0, 0.0, 1.0] + [0.0] * 1532,
            "business": [0.0, 0.0, 0.0, 0.0, 1.0] + [0.0] * 1531,
            "mixed topic": [0.5, 0.5] + [0.0] * 1534,  # Between finance and tech
        },
    )
    fastapi_app.state.embedding_generator = embedder

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Setup comprehensive test data
        await _setup_comprehensive_test_data(pg_vector_db_engine, embedder)
        yield client
    fastapi_app.dependency_overrides.clear()


async def _setup_comprehensive_test_data(
    engine: AsyncEngine, embedder: MockEmbeddingGenerator
) -> None:
    """Setup comprehensive test data for vector search testing."""
    db = Database(engine=engine)
    # Create documents with different characteristics
    test_docs = [
        {
            "source_type": "note",
            "source_id": "finance_note",
            "title": "Financial Planning Guide",
            "content": "Investment strategies and portfolio management",
            "query": "finance",
            "metadata": {"category": "finance", "priority": "high"},
        },
        {
            "source_type": "pdf",
            "source_id": "tech_report",
            "title": "Technology Trends 2024",
            "content": "AI and machine learning developments",
            "query": "technology",
            "metadata": {"category": "technology", "year": 2024},
        },
        {
            "source_type": "email",
            "source_id": "health_newsletter",
            "title": "Health Tips Newsletter",
            "content": "Nutrition and exercise recommendations",
            "query": "health",
            "metadata": {"category": "health", "newsletter": True},
        },
        {
            "source_type": "web_page",
            "source_id": "edu_article",
            "title": "Online Learning Best Practices",
            "content": "Distance education methodologies",
            "query": "education",
            "metadata": {"category": "education", "difficulty": "intermediate"},
        },
        {
            "source_type": "note",
            "source_id": "business_plan",
            "title": "Startup Business Plan",
            "content": "Market analysis and revenue projections",
            "query": "business",
            "metadata": {"category": "business", "confidential": True},
        },
    ]

    for doc_data in test_docs:
        # Create document
        doc = TestDocument(
            source_type=doc_data["source_type"],
            source_id=doc_data["source_id"],
            id=None,
            source_uri=f"test://{doc_data['source_id']}",
            title=doc_data["title"],
            created_at=datetime.now(UTC),
            metadata=doc_data["metadata"],
            file_path=None,
        )
        doc_id = await db.vector.add_document(doc)

        # Add embedding
        embedding = embedder.embedding_map[doc_data["query"]]
        await db.vector.add_embedding(
            document_id=doc_id,
            chunk_index=0,
            embedding_type="content_chunk",
            embedding=embedding,
            embedding_model="gemini-exp-03-07",  # Use correct model name
            content=doc_data["content"],
        )


async def _add_embedded_document(
    db: Database,
    source_type: str,
    source_id: str,
    created_at: datetime,
    embedding: list[float],
) -> None:
    doc_id = await db.vector.add_document(
        TestDocument(
            source_type=source_type,
            source_id=source_id,
            title=source_id,
            created_at=created_at,
        )
    )
    await db.vector.add_embedding(
        document_id=doc_id,
        chunk_index=0,
        embedding_type="content_chunk",
        embedding=embedding,
        embedding_model="gemini-exp-03-07",
        content=f"Content of {source_id}",
    )


# ast-grep-ignore: no-dict-any - external API response has dynamic fields
def _source_ids(results: list[dict[str, Any]]) -> list[str]:
    return [result["document"]["source_id"] for result in results]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_semantic_accuracy(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test that semantic search returns relevant results in correct order."""
    # Test finance query
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "finance", "limit": 10}
    )
    assert resp.status_code == 200
    results = resp.json()

    assert len(results) == len(FIXTURE_SOURCE_IDS)
    top_result = results[0]
    assert top_result["document"]["source_id"] == "finance_note"
    assert top_result["score"] == pytest.approx(1.0, abs=1e-6)
    assert all(result["score"] < top_result["score"] for result in results[1:])


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_filters_by_source_type(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test filtering by source type."""
    # Search only in notes
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/",
        json={
            "query_text": "technology",
            "limit": 10,
            "filters": {"source_types": ["note"]},
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    assert sorted(_source_ids(results)) == ["business_plan", "finance_note"]
    assert all(result["document"]["source_type"] == "note" for result in results)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_metadata_filtering(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test filtering by metadata."""
    # Filter by category
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/",
        json={
            "query_text": "technology",
            "limit": 10,
            "filters": {"metadata_filters": {"category": "technology"}},
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    assert _source_ids(results) == ["tech_report"]
    assert results[0]["document"]["metadata"]["category"] == "technology"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_created_before_excludes_newer_documents(
    comprehensive_vector_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """created_before keeps only documents created on or before the cutoff."""
    await _add_embedded_document(
        Database(engine=pg_vector_db_engine),
        source_type="pdf",
        source_id="archived_report",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        embedding=TECHNOLOGY_EMBEDDING,
    )

    resp = await comprehensive_vector_client.post(
        "/api/vector-search/",
        json={
            "query_text": "technology",
            "limit": 10,
            "filters": {"created_before": "2021-01-01T00:00:00+00:00"},
        },
    )

    assert resp.status_code == 200
    results = resp.json()
    assert _source_ids(results) == ["archived_report"]
    assert datetime.fromisoformat(results[0]["document"]["created_at"]) <= datetime(
        2021, 1, 1, tzinfo=UTC
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_created_after_excludes_older_documents(
    comprehensive_vector_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """created_after keeps only documents created on or after the cutoff."""
    await _add_embedded_document(
        Database(engine=pg_vector_db_engine),
        source_type="pdf",
        source_id="archived_report",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        embedding=TECHNOLOGY_EMBEDDING,
    )

    resp = await comprehensive_vector_client.post(
        "/api/vector-search/",
        json={
            "query_text": "technology",
            "limit": 10,
            "filters": {"created_after": "2021-01-01T00:00:00+00:00"},
        },
    )

    assert resp.status_code == 200
    assert sorted(_source_ids(resp.json())) == FIXTURE_SOURCE_IDS


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_empty_query(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """A blank query returns no results rather than an error."""
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "", "limit": 5}
    )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_invalid_limit(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test behavior with invalid limit values."""
    # Test negative limit
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "technology", "limit": -1}
    )
    assert resp.status_code == 422  # Validation error

    # Test zero limit
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "technology", "limit": 0}
    )
    assert resp.status_code == 422  # Validation error


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_very_large_limit(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test behavior with very large limit."""
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "technology", "limit": 10000}
    )
    assert resp.status_code == 200
    assert sorted(_source_ids(resp.json())) == FIXTURE_SOURCE_IDS


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_malformed_request(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test behavior with malformed requests."""
    # Missing required query_text
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"limit": 5}
    )
    assert resp.status_code == 422  # Validation error

    # Invalid JSON structure
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/",
        json={"query_text": "test", "filters": "invalid_filters_should_be_object"},
    )
    assert resp.status_code == 422  # Validation error


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_special_characters(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test search with special characters and Unicode."""
    special_queries = [
        "café résumé",  # Accented characters
        "数据库查询",  # Chinese characters
        "test@example.com",  # Email format
        "file:///path/to/file",  # URI format
        "SELECT * FROM table;",  # SQL injection attempt
        "<script>alert('xss')</script>",  # XSS attempt
    ]

    for query in special_queries:
        resp = await comprehensive_vector_client.post(
            "/api/vector-search/", json={"query_text": query, "limit": 5}
        )
        assert resp.status_code == 200, (query, resp.text)
        assert sorted(_source_ids(resp.json())) == FIXTURE_SOURCE_IDS, query


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_concurrent_requests(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Concurrent searches each rank their own matching document first."""
    expected_top_match = {
        "finance": "finance_note",
        "technology": "tech_report",
        "health": "health_newsletter",
        "education": "edu_article",
        "business": "business_plan",
    }

    async def top_match(query: str) -> str:
        resp = await comprehensive_vector_client.post(
            "/api/vector-search/", json={"query_text": query, "limit": 5}
        )
        assert resp.status_code == 200, (query, resp.text)
        return resp.json()[0]["document"]["source_id"]

    top_matches = await asyncio.gather(*(top_match(q) for q in expected_top_match))

    assert dict(zip(expected_top_match, top_matches, strict=True)) == (
        expected_top_match
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_document_with_no_embeddings(
    comprehensive_vector_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """Test document detail for document without embeddings."""
    db = Database(engine=pg_vector_db_engine)
    # Create document without embeddings
    doc = TestDocument(
        source_type="orphan",
        source_id="no_embeddings",
        id=None,
        source_uri=None,
        title="Document Without Embeddings",
        created_at=datetime.now(UTC),
        metadata={"orphan": True},
        file_path=None,
    )
    doc_id = await db.vector.add_document(doc)

    # Should still be able to get document details
    resp = await comprehensive_vector_client.get(
        f"/api/vector-search/document/{doc_id}"
    )
    assert resp.status_code == 200
    doc_detail = resp.json()
    assert doc_detail["title"] == "Document Without Embeddings"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_large_dataset_returns_nearest_documents_up_to_limit(
    comprehensive_vector_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """With more documents than the limit, the nearest ones come back in order."""
    db = Database(engine=pg_vector_db_engine)
    # Each document sits at a strictly larger angle from the "finance" axis
    # than the previous one, and all are nearer to it than tech_report (at 90
    # degrees), so the nearest 20 to "finance" are known and strictly ordered.
    for i in range(50):
        angle = (i + 1) * (math.pi / 2) / 52
        await _add_embedded_document(
            db,
            source_type="performance_test",
            source_id=f"perf_doc_{i}",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            embedding=[math.cos(angle), math.sin(angle)] + [0.0] * 1534,
        )

    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "finance", "limit": 20}
    )

    assert resp.status_code == 200
    assert _source_ids(resp.json()) == [
        "finance_note",
        *(f"perf_doc_{i}" for i in range(19)),
    ]
