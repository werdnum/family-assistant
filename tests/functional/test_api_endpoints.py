from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import get_db


@pytest.fixture
async def api_client(
    db_engine: AsyncEngine,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[DatabaseContext, None]:
        async with DatabaseContext(engine=db_engine) as db:
            yield db

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.state.embedding_generator = MockEmbeddingGenerator(
        model_name="test", dimensions=3
    )
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
    fastapi_app.dependency_overrides.clear()


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
        doc = SimpleNamespace(
            id=None,
            source_type="note",
            source_id="doc1",
            source_uri=None,
            title="Doc One",
            created_at=datetime.now(timezone.utc),
            metadata=None,
        )
        doc_id = await db.vector.add_document(doc)

    resp = await api_client.get("/api/documents/")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert any(d["id"] == doc_id for d in docs)

    resp = await api_client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == doc_id


@pytest.mark.asyncio
@pytest.mark.skip("Vector search requires full vector DB setup")
async def test_vector_search_api_search(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    embedder = MockEmbeddingGenerator(
        model_name="test",
        dimensions=3,
        embedding_map={
            "query": [0.1, 0.2, 0.3],
            "content": [0.1, 0.2, 0.3],
        },
    )
    fastapi_app.state.embedding_generator = embedder

    async with DatabaseContext(engine=db_engine) as db:
        await db.vector.init_db()
        doc = SimpleNamespace(
            id=None,
            source_type="note",
            source_id="doc2",
            source_uri=None,
            title="Doc Two",
            created_at=datetime.now(timezone.utc),
            metadata=None,
        )
        doc_id = await db.vector.add_document(doc)
        await db.vector.add_embedding(
            document_id=doc_id,
            chunk_index=0,
            embedding_type="content_chunk",
            embedding=[0.1, 0.2, 0.3],
            embedding_model="test",
            content="content",
        )

    resp = await api_client.post("/api/vector-search/", json={"query_text": "query"})
    assert resp.status_code == 200
    results = resp.json()
    assert any(r["document"]["id"] == doc_id for r in results)
