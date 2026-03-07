"""
Typed data structures for the indexing subsystem.
"""

from typing import TypedDict


class EmailMetadata(TypedDict, total=False):
    """Metadata for an email document."""

    sender_address: str
    from_header: str
    recipient_address: str
    to_header: str
    cc_header: str
    mailgun_timestamp: float
    headers: dict[str, str]


class EmailAttachmentInfo(TypedDict):
    """Information about an email attachment."""

    filename: str
    content_type: str
    size: int
    storage_path: str


class ChunkMetadata(TypedDict, total=False):
    """Metadata for a text chunk."""

    original_source: str
    chunk_index: int
    total_chunks: int
    original_embedding_type: str
    original_content_length: int
    chunk_content_length: int


class UrlScrapeMetadata(TypedDict, total=False):
    """Metadata for a scraped URL."""

    original_url: str
    fetched_title: str
    source_scraper_description: str


class RawFileMetadata(TypedDict, total=False):
    """Metadata for a raw file."""

    original_filename: str
    email_db_id: int
    email_source_id: str


class ExtractionMetadata(TypedDict):
    """Metadata for extracted content."""

    extraction_method: str


class EmbeddingMetadata(TypedDict):
    """Metadata for a single embedding item in a batch embedding task."""

    embedding_type: str
    chunk_index: int
    original_content_metadata: dict[str, object]
    content_hash: str | None


class EmbedAndStoreBatchPayload(TypedDict):
    """Payload for the embed_and_store_batch task."""

    document_id: int
    texts_to_embed: list[str]
    embedding_metadata_list: list[EmbeddingMetadata]


class IngestionResult(TypedDict, total=False):
    """Result of a document ingestion request."""

    message: str
    document_id: int | None
    task_enqueued: bool
    error_detail: str | None
    status_code: int


# A union of all possible metadata types for IndexableContent
IndexableContentMetadata = (
    ChunkMetadata
    | UrlScrapeMetadata
    | RawFileMetadata
    | ExtractionMetadata
    | dict[str, object]
)
