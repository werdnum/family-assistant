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


class ChunkMetadata(TypedDict):
    """Metadata for a text chunk."""

    original_source: str
    chunk_index: int
    total_chunks: int


class UrlScrapeMetadata(TypedDict):
    """Metadata for a scraped URL."""

    original_url: str


class RawFileMetadata(TypedDict):
    """Metadata for a raw file."""

    original_filename: str
    email_db_id: int
    email_source_id: str


# A union of all possible metadata types for IndexableContent
IndexableContentMetadata = (
    ChunkMetadata | UrlScrapeMetadata | RawFileMetadata | dict[str, object]
)
