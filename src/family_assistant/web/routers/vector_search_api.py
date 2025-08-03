from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import DocumentRecord, get_document_by_id
from family_assistant.storage.vector_search import (
    MetadataFilter,
    VectorSearchQuery,
    query_vector_store,
)
from family_assistant.web.dependencies import (
    get_db,
    get_embedding_generator_dependency,
)

vector_search_api_router = APIRouter()


class SearchFilters(BaseModel):
    source_types: list[str] = []
    embedding_types: list[str] = []
    created_after: datetime | None = None
    created_before: datetime | None = None
    title_like: str | None = None
    metadata_filters: dict[str, str] = {}


class SearchRequest(BaseModel):
    query_text: str
    limit: int = Field(
        default=5, gt=0, description="Number of results to return (must be positive)"
    )
    filters: SearchFilters | None = None


class SearchResultDocument(BaseModel):
    id: int
    title: str | None = None
    source_type: str
    source_id: str
    source_uri: str | None = None
    created_at: datetime | None = None
    metadata: dict | None = None


class SearchResult(BaseModel):
    document: SearchResultDocument
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
    # Use default filters if none provided
    filters = payload.filters or SearchFilters()

    # Build metadata filters from dict
    metadata_filters = []
    if filters.metadata_filters:
        for key, value in filters.metadata_filters.items():
            metadata_filters.append(MetadataFilter(key=key, value=value))

    query = VectorSearchQuery(
        semantic_query=payload.query_text,
        embedding_model=embedding_generator.model_name,
        search_type="semantic",
        limit=payload.limit,
        # Apply filters
        source_types=filters.source_types,
        embedding_types=filters.embedding_types,
        created_after=filters.created_after,
        created_before=filters.created_before,
        title_like=filters.title_like,
        metadata_filters=metadata_filters,
    )
    embed_result = await embedding_generator.generate_embeddings([payload.query_text])
    query_embedding = embed_result.embeddings[0] if embed_result.embeddings else None
    results = await query_vector_store(db_context, query, query_embedding)

    # Transform raw results to API format
    transformed_results = []
    for result in results:
        document = SearchResultDocument(
            id=result["document_id"],
            title=result["title"],
            source_type=result["source_type"],
            source_id=result["source_id"],
            source_uri=result["source_uri"],
            created_at=result["created_at"],
            metadata=result["doc_metadata"],
        )
        # Convert distance to score (lower distance = higher score)
        # Use 1 / (1 + distance) to convert distance to score between 0 and 1
        # Missing distance should get a low score, not perfect score
        distance = result.get("distance")
        score = 1.0 / (1.0 + distance) if distance is not None else 0.0

        transformed_results.append(SearchResult(document=document, score=score))

    return transformed_results


class DocumentDetail(BaseModel):
    id: int
    source_type: str
    source_id: str
    source_uri: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    added_at: datetime
    metadata: dict | None = None


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
        metadata=record.doc_metadata,
    )
