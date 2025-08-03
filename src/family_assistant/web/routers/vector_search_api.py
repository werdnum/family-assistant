from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import DocumentRecord, get_document_by_id
from family_assistant.storage.vector_search import (
    VectorSearchQuery,
    query_vector_store,
)
from family_assistant.web.dependencies import (
    get_db,
    get_embedding_generator_dependency,
)

vector_search_api_router = APIRouter()


class SearchRequest(BaseModel):
    query_text: str
    limit: int = 5


class SearchResult(BaseModel):
    document: dict[str, Any]
    score: float


@vector_search_api_router.post("/")
async def search_documents_api(
    payload: SearchRequest,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    embedding_generator: Annotated[
        EmbeddingGenerator, Depends(get_embedding_generator_dependency)
    ],
) -> list[SearchResult]:
    """Search indexed documents."""
    query = VectorSearchQuery(
        semantic_query=payload.query_text,
        embedding_model=embedding_generator.model_name,
        search_type="semantic",
        limit=payload.limit,
    )
    embed_result = await embedding_generator.generate_embeddings([payload.query_text])
    query_embedding = embed_result.embeddings[0] if embed_result.embeddings else None
    results = await query_vector_store(db_context, query, query_embedding)
    return [
        SearchResult(document=result["document"], score=result["score"])
        for result in results
    ]


class DocumentDetail(BaseModel):
    id: int
    source_type: str
    source_id: str
    source_uri: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    added_at: datetime
    doc_metadata: dict | None = None


@vector_search_api_router.get("/document/{document_id}")
async def get_document_detail(
    document_id: int, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> DocumentDetail:
    """Return document metadata."""
    record: DocumentRecord | None = await get_document_by_id(db_context, document_id)
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return DocumentDetail(
        id=record.id,
        source_type=record.source_type,
        source_id=record.source_id,
        source_uri=record.source_uri,
        title=record.title,
        created_at=record.created_at,
        added_at=record.added_at,
        doc_metadata=record.doc_metadata,
    )
