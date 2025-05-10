from datetime import datetime
from typing import Any

from pydantic import BaseModel


# --- Pydantic model for search results (optional but good practice) ---
class SearchResultItem(BaseModel):
    embedding_id: int
    document_id: int
    title: str | None
    source_type: str
    source_id: str | None = None
    source_uri: str | None = None
    created_at: datetime | None
    embedding_type: str
    embedding_source_content: str | None
    chunk_index: int | None = None
    doc_metadata: dict[str, Any] | None = None
    distance: float | None = None
    fts_score: float | None = None
    rrf_score: float | None = None

    class Config:
        orm_mode = True  # Allows creating from ORM-like objects (dict-like rows)


# --- Pydantic model for API response ---
class DocumentUploadResponse(BaseModel):
    message: str
    document_id: int
    task_enqueued: bool
