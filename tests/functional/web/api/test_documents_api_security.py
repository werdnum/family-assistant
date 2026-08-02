"""Security regression tests for document APIs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from family_assistant.indexing.message_history_indexer import (
    MESSAGE_HISTORY_SOURCE_TYPE,
)
from family_assistant.storage.database import Database
from family_assistant.storage.vector import add_document, add_embedding

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class _TestDocument:
    source_type: str
    source_id: str
    source_uri: str | None
    title: str | None
    created_at: datetime | None
    # ast-grep-ignore: no-dict-any - test document metadata mirrors the vector Document protocol
    metadata: dict[str, Any] | None
    file_path: str | None
    visibility_labels: list[str] | None
    id: int | None = None


async def test_document_apis_hide_message_history_documents(
    db_engine: AsyncEngine,
    api_test_client: httpx.AsyncClient,
) -> None:
    """Generic document APIs must not expose indexed message-history artifacts."""
    db = Database(engine=db_engine)
    public_document_id = await add_document(
        db,
        _TestDocument(
            source_type="note",
            source_id="public-note",
            source_uri=None,
            title="Public note",
            created_at=datetime.now(UTC),
            metadata={"kind": "public"},
            file_path=None,
            visibility_labels=[],
        ),
    )
    message_history_document_id = await add_document(
        db,
        _TestDocument(
            source_type=MESSAGE_HISTORY_SOURCE_TYPE,
            source_id="message_turn:secret-turn",
            source_uri=None,
            title="Secret message-history turn",
            created_at=datetime.now(UTC),
            metadata={"conversation_id": "private-conversation"},
            file_path=None,
            visibility_labels=[],
        ),
    )
    await add_embedding(
        db_context=db,
        document_id=message_history_document_id,
        chunk_index=0,
        embedding_type="message_turn",
        embedding=[0.1, 0.2, 0.3],
        embedding_model="test-model",
        content="private conversation text",
    )

    list_response = await api_test_client.get("/api/documents/")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["total"] == 1
    assert [item["id"] for item in list_payload["documents"]] == [public_document_id]

    filtered_response = await api_test_client.get(
        "/api/documents/",
        params={"source_type": MESSAGE_HISTORY_SOURCE_TYPE},
    )
    assert filtered_response.status_code == 200
    assert filtered_response.json() == {"documents": [], "total": 0}

    public_response = await api_test_client.get(f"/api/documents/{public_document_id}")
    assert public_response.status_code == 200

    hidden_response = await api_test_client.get(
        f"/api/documents/{message_history_document_id}"
    )
    assert hidden_response.status_code == 404

    reindex_response = await api_test_client.post(
        f"/api/documents/{message_history_document_id}/reindex"
    )
    assert reindex_response.status_code == 404

    vector_detail_response = await api_test_client.get(
        f"/api/vector-search/document/{message_history_document_id}"
    )
    assert vector_detail_response.status_code == 404
