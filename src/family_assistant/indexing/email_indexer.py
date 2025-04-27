"""
Handles the indexing process for emails stored in the database.
"""

import asyncio
import logging
from typing import Any, Dict, Optional
from datetime import datetime
from dataclasses import dataclass, field

# Import RowMapping for type hinting in from_row
from sqlalchemy.engine import RowMapping

# Import the Document protocol from the correct location
from family_assistant.storage.vector import Document

logger = logging.getLogger(__name__)


# --- EmailDocument Class (Moved from storage.email) ---


@dataclass(frozen=True)  # Use dataclass for simplicity and immutability
class EmailDocument(Document):
    """
    Represents an email document conforming to the Document protocol
    for vector storage ingestion. Includes methods to convert from
    a received_emails table row.
    """

    _source_id: str
    _title: Optional[str] = None
    _created_at: Optional[datetime] = None
    _source_uri: Optional[str] = None
    _base_metadata: Dict[str, Any] = field(default_factory=dict)
    _content_plain: Optional[str] = None  # Store plain text content separately

    @property
    def source_type(self) -> str:
        """The type of the source ('email')."""
        return "email"

    @property
    def source_id(self) -> str:
        """The unique identifier (Message-ID header)."""
        return self._source_id

    @property
    def source_uri(self) -> Optional[str]:
        """URI or path to the original item (not typically available for emails)."""
        return self._source_uri  # Could potentially be a mail archive link if available

    @property
    def title(self) -> Optional[str]:
        """Title or subject of the document."""
        return self._title

    @property
    def created_at(self) -> Optional[datetime]:
        """Original creation date (from 'Date' header, timezone-aware)."""
        return self._created_at

    @property
    def metadata(self) -> Optional[Dict[str, Any]]:
        """Base metadata extracted directly from the source."""
        return self._base_metadata

    @property
    def content_plain(self) -> Optional[str]:
        """The plain text content of the email (e.g., stripped_text)."""
        return self._content_plain

    @classmethod
    def from_row(cls, row: RowMapping) -> "EmailDocument":
        """
        Creates an EmailDocument instance from a SQLAlchemy RowMapping
        representing a row from the received_emails table.
        """
        # Ensure required fields are present
        message_id = row.get("message_id_header")
        if not message_id:
            raise ValueError(
                "Cannot create EmailDocument: 'message_id_header' is missing from row."
            )

        # Extract base metadata
        base_metadata = {
            key: row.get(key)
            for key in [
                "sender_address",
                "from_header",
                "recipient_address",
                "to_header",
                "cc_header",
                "mailgun_timestamp",  # Include mailgun timestamp if useful
            ]
            if row.get(key) is not None  # Only include if not None
        }
        # Add headers JSON if present and not None
        headers_json = row.get("headers_json")
        if headers_json:
            base_metadata["headers"] = (
                headers_json  # Store raw headers under 'headers' key
            )

        # Prefer stripped_text for cleaner content
        content = row.get("stripped_text") or row.get("body_plain")

        return cls(
            _source_id=message_id,
            _title=row.get("subject"),
            _created_at=row.get("email_date"),  # Already parsed to datetime or None
            _base_metadata=base_metadata,
            _content_plain=content,
            # _source_uri could be set if a web view link exists, otherwise None
        )

    def to_dict(self) -> Dict[str, Any]:
        """Converts the EmailDocument instance to a dictionary."""
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_uri": self.source_uri,
            "title": self.title,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.metadata,
            "content_plain": self.content_plain,
        }


# --- Task Handler Placeholder ---
# TODO: Implement the actual indexing logic here
async def handle_index_email(db_context: Any, payload: Dict[str, Any]):
    logger.warning(f"handle_index_email called with payload: {payload}, but is not implemented yet.")
    await asyncio.sleep(1) # Simulate work


# --- Dependency Injection Placeholder ---
# TODO: Implement proper dependency injection
def set_indexing_dependencies(embedding_generator: Any, llm_client: Optional[Any] = None):
    logger.warning("set_indexing_dependencies called, but dependencies are not used yet.")

__all__ = ["EmailDocument", "handle_index_email", "set_indexing_dependencies"]
