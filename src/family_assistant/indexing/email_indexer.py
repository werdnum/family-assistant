"""
Handles the indexing process for emails stored in the database.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, List  # Added List
from datetime import datetime
from dataclasses import dataclass, field

# Import RowMapping for type hinting in from_row
from sqlalchemy.engine import RowMapping
from sqlalchemy import select, update  # Import select and update
from sqlalchemy.exc import SQLAlchemyError

# Use absolute imports
from family_assistant import storage # For DB operations (add_document)
from family_assistant.indexing.pipeline import IndexingPipeline, IndexableContent
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import (
    received_emails_table,
)  # Import table definition
# Import the Document protocol from the correct location
from family_assistant.storage.vector import Document, get_document_by_id
from family_assistant.tools import ToolExecutionContext  # Import the context class

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


# --- Dependencies (Set via set_indexing_dependencies) ---
indexing_pipeline_instance: Optional[IndexingPipeline] = None # Correctly defined

# --- Task Handler Implementation ---
async def handle_index_email(
    exec_context: ToolExecutionContext, payload: Dict[str, Any]
):
    """
    Task handler to index a specific email from the received_emails table.
    Receives ToolExecutionContext from the TaskWorker.
    """
    # Extract db_context from the execution context
    db_context = exec_context.db_context
    if not db_context:
        logger.error(
            "DatabaseContext not found in ToolExecutionContext for handle_index_email."
        )
        raise ValueError("Missing DatabaseContext dependency in context.")

    email_db_id = payload.get("email_db_id")
    if not email_db_id:
        raise ValueError("Missing 'email_db_id' in index_email task payload.")

    if not indexing_pipeline_instance:
        raise RuntimeError("IndexingPipeline dependency not set for email indexing.")

    logger.info(f"Starting indexing for email DB ID: {email_db_id}")

    # --- 1. Fetch Email Data ---
    # No need to update status here, task status handles it
    select_stmt = select(received_emails_table).where(
        received_emails_table.c.id == email_db_id
    )
    email_row = await db_context.fetch_one(select_stmt)

    if not email_row:
        # Email might have been deleted between enqueueing and processing
        logger.warning(f"Email {email_db_id} not found in database. Skipping indexing.")
        # Don't raise an error, just exit gracefully. Task will be marked 'done'.
        return

    # --- 2. Create Document Object ---
    try:
        email_doc = EmailDocument.from_row(email_row)
    except ValueError as e:
        logger.error(f"Failed to create EmailDocument for DB ID {email_db_id}: {e}")
        raise  # Re-raise to mark task as failed

    # --- 3. (Skipped) Enrich Metadata ---
    enriched_metadata = None
    # LLM enrichment logic would go here in the future

    # --- 4. Add/Update Document Record in Vector DB & Get DB Record ---
    doc_db_id = await storage.add_document(
        db_context=db_context, doc=email_doc, enriched_doc_metadata=enriched_metadata
    )
    logger.info(
        f"Added/Updated document record for email {email_db_id}, vector DB doc ID: {doc_db_id}"
    )

    try:
        db_document_record = await get_document_by_id(db_context, doc_db_id)
        if not db_document_record:
            # This should ideally not happen if add_document succeeded
            raise ValueError(f"Failed to retrieve document record for ID {doc_db_id} after adding/updating.")
    except SQLAlchemyError as e:
        logger.error(f"Database error fetching document record {doc_db_id}: {e}", exc_info=True)
        raise RuntimeError(f"Failed to fetch document record {doc_db_id}") from e

    # --- 5. Prepare Initial Content for Pipeline ---
    initial_items: List[IndexableContent] = []
    if email_doc.content_plain:
        # The pipeline will handle title extraction, chunking, summarizing, etc.
        # Provide the raw plain text body.
        plain_text_item = IndexableContent(
            content=email_doc.content_plain,
            embedding_type="raw_body_text", # A generic type for processors to pick up
            mime_type="text/plain",
            source_processor="EmailIndexer.handle_index_email",
            metadata={"original_source": "email_body"},
        )
        initial_items.append(plain_text_item)

    if not initial_items:
        logger.warning(
            f"No text content (e.g., plain body) found to pass to pipeline for email {email_db_id}. Skipping pipeline run."
        )
        # Task is considered done as the document record was created/updated.
        return

    # --- 6. Run Indexing Pipeline ---
    try:
        logger.info(f"Running indexing pipeline for email {email_db_id} (Doc ID: {doc_db_id}) with {len(initial_items)} initial items.")
        await indexing_pipeline_instance.run(
            initial_items=initial_items,
            original_document=db_document_record, # Pass the DB record
            context=exec_context,
        )
    except Exception as e:
        logger.error(f"Indexing pipeline run failed for email {email_db_id} (Doc ID: {doc_db_id}): {e}", exc_info=True)
        raise RuntimeError(f"Indexing pipeline failed for email {email_db_id}") from e

    logger.info(f"Indexing pipeline successfully initiated for email {email_db_id} (Doc ID: {doc_db_id}).")
    # Task completion is handled by the worker loop


# --- Dependency Injection ---
def set_indexing_dependencies(
    pipeline: IndexingPipeline,
):
    """Sets the necessary dependencies for the email indexer."""
    global indexing_pipeline_instance
    indexing_pipeline_instance = pipeline
    logger.info("Indexing dependencies set (IndexingPipeline).")


__all__ = ["EmailDocument", "handle_index_email", "set_indexing_dependencies"]
