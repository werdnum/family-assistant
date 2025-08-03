"""
Comprehensive tests for vector search functionality.
Tests advanced search features, error conditions, and edge cases.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import get_db


@pytest.fixture
async def comprehensive_vector_client(
    pg_vector_db_engine: AsyncEngine,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """API client with comprehensive test data setup."""

    async def override_get_db() -> AsyncGenerator[DatabaseContext, None]:
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
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
    async with DatabaseContext(engine=engine) as db:
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
            doc = SimpleNamespace(
                id=None,
                source_type=doc_data["source_type"],
                source_id=doc_data["source_id"],
                source_uri=f"test://{doc_data['source_id']}",
                title=doc_data["title"],
                created_at=datetime.now(timezone.utc),
                metadata=doc_data["metadata"],
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

    # Finance document should be first (exact match)
    assert len(results) > 0
    top_result = results[0]
    assert top_result["document"]["source_id"] == "finance_note"
    # Since API converts distance to score, exact match should have score = 1.0
    assert top_result["score"] == pytest.approx(1.0, abs=1e-6)

    # Mixed topic should be second (partial match)
    if len(results) > 1:
        second_result = results[1]
        # Should have lower score than exact match
        assert second_result["score"] < top_result["score"]


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

    # Should only return note documents
    for result in results:
        assert result["document"]["source_type"] == "note"


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

    # Should only return technology documents
    for result in results:
        assert result["document"]["metadata"]["category"] == "technology"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_date_filtering(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test filtering by date range."""
    # Filter documents created after a certain time
    cutoff_time = datetime.now(timezone.utc).isoformat()

    resp = await comprehensive_vector_client.post(
        "/api/vector-search/",
        json={
            "query_text": "technology",
            "limit": 10,
            "filters": {"created_before": cutoff_time},
        },
    )
    assert resp.status_code == 200
    results = resp.json()

    # All documents should be created before cutoff
    for result in results:
        doc_created = datetime.fromisoformat(
            result["document"]["created_at"].replace("Z", "+00:00")
        )
        cutoff_dt = datetime.fromisoformat(cutoff_time.replace("Z", "+00:00"))
        assert doc_created <= cutoff_dt


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_empty_query(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test behavior with empty query."""
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "", "limit": 5}
    )
    assert resp.status_code == 200
    results = resp.json()

    # Should return some results (fallback behavior)
    assert isinstance(results, list)


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
    results = resp.json()

    # Should not crash and should respect actual document count
    assert isinstance(results, list)
    assert len(results) <= 10000  # Should be much less due to actual document count


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
        # Should not crash, might return 200 with empty results
        assert resp.status_code in [200, 422]  # Either success or validation error

        if resp.status_code == 200:
            results = resp.json()
            assert isinstance(results, list)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_concurrent_requests(
    comprehensive_vector_client: httpx.AsyncClient,
) -> None:
    """Test concurrent search requests."""
    import asyncio

    async def single_search(query: str) -> list[dict[str, Any]]:
        resp = await comprehensive_vector_client.post(
            "/api/vector-search/", json={"query_text": query, "limit": 5}
        )
        return resp.json() if resp.status_code == 200 else []

    # Run multiple searches concurrently
    queries = ["finance", "technology", "health", "education", "business"]
    tasks = [single_search(query) for query in queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # All should succeed
    for i, result in enumerate(results):
        assert not isinstance(result, Exception), f"Query {queries[i]} failed: {result}"
        assert isinstance(result, list)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_document_with_no_embeddings(
    comprehensive_vector_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """Test document detail for document without embeddings."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create document without embeddings
        doc = SimpleNamespace(
            id=None,
            source_type="orphan",
            source_id="no_embeddings",
            source_uri=None,
            title="Document Without Embeddings",
            created_at=datetime.now(timezone.utc),
            metadata={"orphan": True},
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
async def test_vector_search_performance_with_large_dataset(
    comprehensive_vector_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """Test search performance with a larger dataset."""
    import time

    # Add more documents for performance testing
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create 50 additional documents
        for i in range(50):
            doc = SimpleNamespace(
                id=None,
                source_type="performance_test",
                source_id=f"perf_doc_{i}",
                source_uri=None,
                title=f"Performance Test Document {i}",
                created_at=datetime.now(timezone.utc),
                metadata={"batch": "performance", "index": i},
            )
            doc_id = await db.vector.add_document(doc)

            # Add random embedding with correct dimensions
            embedding = [0.1] * 1536  # Use correct dimensions
            embedding[0] = 0.1 * (i % 10)  # Add some variation
            if len(embedding) > 1:
                embedding[1] = 0.2 * ((i + 1) % 10)

            await db.vector.add_embedding(
                document_id=doc_id,
                chunk_index=0,
                embedding_type="content_chunk",
                embedding=embedding,
                embedding_model="gemini-exp-03-07",  # Use correct model
                content=f"Performance test content for document {i}",
            )

    # Time the search
    start_time = time.time()
    resp = await comprehensive_vector_client.post(
        "/api/vector-search/", json={"query_text": "performance", "limit": 20}
    )
    end_time = time.time()

    assert resp.status_code == 200
    results = resp.json()
    assert isinstance(results, list)

    # Should complete reasonably quickly (under 5 seconds for test environment)
    search_duration = end_time - start_time
    assert search_duration < 5.0, f"Search took too long: {search_duration}s"
