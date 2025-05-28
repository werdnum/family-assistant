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


# --- API Token Models ---
class ApiTokenCreateRequest(BaseModel):
    name: str
    expires_at: str | None = (
        None  # ISO 8601 format string, e.g., "YYYY-MM-DDTHH:MM:SSZ"
    )


class ApiTokenCreateResponse(BaseModel):
    id: int
    name: str
    full_token: str  # The full, unhashed token (prefix + secret)
    prefix: str
    user_identifier: str
    created_at: datetime
    expires_at: datetime | None = None
    is_revoked: bool
    last_used_at: datetime | None = None


# --- API Chat Models ---
class ChatPromptRequest(BaseModel):
    prompt: str
    conversation_id: str | None = None
    profile_id: str | None = None  # Added to specify processing profile


class ChatMessageResponse(BaseModel):
    reply: str
    conversation_id: str
    turn_id: str
