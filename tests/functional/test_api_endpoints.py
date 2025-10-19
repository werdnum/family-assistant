from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import get_db


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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def metadata(self) -> dict[str, Any] | None:
        return self._metadata

    @property
    def file_path(self) -> str | None:
        return self._file_path


@pytest.mark.asyncio
async def test_notes_api_crud(api_client: httpx.AsyncClient) -> None:
    resp = await api_client.post(
        "/api/notes/",
        json={"title": "Note1", "content": "Hello", "include_in_prompt": True},
    )
    assert resp.status_code == 201

    resp = await api_client.get("/api/notes/")
    assert resp.status_code == 200
    notes = resp.json()
    assert any(n["title"] == "Note1" for n in notes)

    resp = await api_client.delete("/api/notes/Note1")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tasks_api_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        await db.tasks.enqueue(task_id="t1", task_type="demo", payload={})

    resp = await api_client.get("/api/tasks/")
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert any(t["task_id"] == "t1" for t in tasks)


@pytest.mark.asyncio
async def test_events_api_list_and_detail(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        await db.events.store_event(source_id="indexing", event_data={"a": 1})
        events, _ = await db.events.get_events_with_listeners()
        event_id = events[0]["event_id"]

    resp = await api_client.get("/api/events/")
    assert resp.status_code == 200
    event_list = resp.json()["events"]
    assert any(e["event_id"] == event_id for e in event_list)

    resp = await api_client.get(f"/api/events/{event_id}")
    assert resp.status_code == 200
    assert resp.json()["event_id"] == event_id


@pytest.mark.asyncio
async def test_documents_api_list_and_detail(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        doc = TestDocument(
            source_type="note",
            source_id="doc1",
            id=None,
            source_uri=None,
            title="Doc One",
            created_at=datetime.now(timezone.utc),
            metadata=None,
            file_path=None,
        )
        doc_id = await db.vector.add_document(doc)

    resp = await api_client.get("/api/documents/")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert any(d["id"] == doc_id for d in docs)

    resp = await api_client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == doc_id


@pytest.fixture
async def vector_api_client(
    pg_vector_db_engine: AsyncEngine,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """API client with PostgreSQL vector DB setup."""

    async def override_get_db() -> AsyncGenerator[DatabaseContext, None]:
        async with DatabaseContext(engine=pg_vector_db_engine) as db:
            yield db

    # Create embedder with dimensions that match PostgreSQL indexes
    embedder = MockEmbeddingGenerator(
        model_name="gemini-exp-03-07",  # Use model name that has index
        dimensions=1536,  # Match expected dimension
        embedding_map={
            "query": [0.1] * 1536,  # Create proper dimension vector
            "content": [0.1] * 1536,  # Create proper dimension vector
        },
    )

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.state.embedding_generator = embedder

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
    fastapi_app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_api_search(
    vector_api_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """Test vector search API endpoint."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create test document
        doc = TestDocument(
            source_type="note",
            source_id="doc2",
            id=None,
            source_uri=None,
            title="Doc Two",
            created_at=datetime.now(timezone.utc),
            metadata=None,
            file_path=None,
        )
        doc_id = await db.vector.add_document(doc)
        # Use correct dimensions and model name
        await db.vector.add_embedding(
            document_id=doc_id,
            chunk_index=0,
            embedding_type="content_chunk",
            embedding=[0.1] * 1536,  # Match expected dimensions
            embedding_model="gemini-exp-03-07",  # Match expected model
            content="content",
        )

    # Test basic search
    resp = await vector_api_client.post(
        "/api/vector-search/", json={"query_text": "query"}
    )
    assert resp.status_code == 200
    results = resp.json()
    assert isinstance(results, list)
    assert any(r["document"]["id"] == doc_id for r in results)

    # Verify result structure matches API schema
    result = next(r for r in results if r["document"]["id"] == doc_id)
    assert "score" in result  # API uses "score", not "distance"
    assert "document" in result
    assert result["document"]["title"] == "Doc Two"
    assert result["document"]["source_type"] == "note"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_api_with_limit(
    vector_api_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """Test vector search API with limit parameter."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create multiple test documents
        doc_ids = []
        for i in range(5):
            doc = TestDocument(
                source_type="note",
                source_id=f"doc{i}",
                id=None,
                source_uri=None,
                title=f"Document {i}",
                created_at=datetime.now(timezone.utc),
                metadata=None,
                file_path=None,
            )
            doc_id = await db.vector.add_document(doc)
            doc_ids.append(doc_id)
            await db.vector.add_embedding(
                document_id=doc_id,
                chunk_index=0,
                embedding_type="content_chunk",
                embedding=[0.1] * 1536,  # Correct dimensions
                embedding_model="gemini-exp-03-07",  # Correct model
                content=f"content {i}",
            )

    # Test with limit
    resp = await vector_api_client.post(
        "/api/vector-search/", json={"query_text": "query", "limit": 3}
    )
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) <= 3


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_api_document_detail(
    vector_api_client: httpx.AsyncClient, pg_vector_db_engine: AsyncEngine
) -> None:
    """Test vector search document detail API endpoint."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        doc = TestDocument(
            source_type="pdf",
            source_id="test_pdf",
            id=None,
            source_uri="file:///test.pdf",
            title="Test PDF Document",
            created_at=datetime.now(timezone.utc),
            metadata={"author": "Test User", "pages": 10},
            file_path=None,
        )
        doc_id = await db.vector.add_document(doc)

    # Test document detail endpoint
    resp = await vector_api_client.get(f"/api/vector-search/document/{doc_id}")
    assert resp.status_code == 200
    doc_detail = resp.json()

    assert doc_detail["id"] == doc_id
    assert doc_detail["title"] == "Test PDF Document"
    assert doc_detail["source_type"] == "pdf"
    assert doc_detail["source_id"] == "test_pdf"
    assert doc_detail["source_uri"] == "file:///test.pdf"
    assert doc_detail["metadata"]["author"] == "Test User"
    assert doc_detail["metadata"]["pages"] == 10


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_vector_search_api_document_not_found(
    vector_api_client: httpx.AsyncClient,
) -> None:
    """Test vector search document detail API with non-existent document."""
    resp = await vector_api_client.get("/api/vector-search/document/99999")
    assert resp.status_code == 404
