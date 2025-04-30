"""
Handles the indexing process for emails stored in the database.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, List # Added List
from datetime import datetime
from dataclasses import dataclass, field

# Import RowMapping for type hinting in from_row
from sqlalchemy.engine import RowMapping
from sqlalchemy import select, update # Import select and update
from sqlalchemy.exc import SQLAlchemyError

# Use absolute imports
from family_assistant import storage # For DB operations (add_document, add_embedding)
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table # Import table definition
from family_assistant.embeddings import EmbeddingGenerator, EmbeddingResult # Protocol for embedding
from family_assistant.llm import LLMInterface # Protocol for LLM (optional enrichment)

# Import the Document protocol from the correct location
from family_assistant.storage.vector import Document
from family_assistant.tools import ToolExecutionContext # Import the context class

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
embedding_generator_instance: Optional[EmbeddingGenerator] = None
llm_client_instance: Optional[LLMInterface] = None # Optional for enrichment


# --- Task Handler Implementation ---
async def handle_index_email(exec_context: ToolExecutionContext, payload: Dict[str, Any]):
    """
    Task handler to index a specific email from the received_emails table.
    Receives ToolExecutionContext from the TaskWorker.
    """
    # Extract db_context from the execution context
    db_context = exec_context.db_context
    if not db_context:
         logger.error("DatabaseContext not found in ToolExecutionContext for handle_index_email.")
         raise ValueError("Missing DatabaseContext dependency in context.")

    email_db_id = payload.get("email_db_id")
    if not email_db_id:
        raise ValueError("Missing 'email_db_id' in index_email task payload.")

    if not embedding_generator_instance:
         raise RuntimeError("EmbeddingGenerator dependency not set for indexing.")
         # Optionally check for llm_client_instance if enrichment is mandatory

    logger.info(f"Starting indexing for email DB ID: {email_db_id}")

    # --- 1. Fetch Email Data ---
    # No need to update status here, task status handles it
    select_stmt = select(received_emails_table).where(received_emails_table.c.id == email_db_id)
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
        raise # Re-raise to mark task as failed

    # --- 3. (Skipped) Enrich Metadata ---
    enriched_metadata = None
    # LLM enrichment logic would go here in the future

    # --- 4. Add/Update Document Record in Vector DB ---
    vector_doc_id = await storage.add_document(
        db_context=db_context,
        doc=email_doc,
        enriched_doc_metadata=enriched_metadata
    )
    logger.info(f"Added/Updated document record for email {email_db_id}, vector DB doc ID: {vector_doc_id}")

    # --- 5. Prepare Texts for Embedding ---
    texts_to_embed: Dict[str, Optional[str]] = {}
    embedding_types: Dict[str, str] = {} # Map text key to embedding type

    if email_doc.title:
        texts_to_embed["title"] = email_doc.title
        embedding_types["title"] = "title"

    if email_doc.content_plain:
        # TODO: Add chunking logic here if needed for long emails
        # For now, embed the whole (stripped) plain text content
        # Consider generating a summary via LLM instead/as well for large content
        texts_to_embed["content_chunk_0"] = email_doc.content_plain # Key includes chunk index
        embedding_types["content_chunk_0"] = "content_chunk"

    if not texts_to_embed:
        logger.warning(f"No text content (title or body) found to embed for email {email_db_id}. Skipping embedding generation.")
        # Task is considered done as the document record was created/updated.
        return

    # --- 6. Generate Embeddings ---
    text_keys = list(texts_to_embed.keys())
    text_values = [texts_to_embed[key] for key in text_keys if texts_to_embed[key] is not None] # Filter out None values just in case

    if not text_values:
         logger.warning(f"All potential texts to embed were None for email {email_db_id}. Skipping embedding generation.")
         return

    logger.info(f"Generating embeddings for {len(text_values)} text part(s) for email {email_db_id} using model {embedding_generator_instance.model_name}...")
    embedding_result: EmbeddingResult = await embedding_generator_instance.generate_embeddings(text_values)

    if len(embedding_result.embeddings) != len(text_values):
        logger.error(f"Mismatch between number of texts ({len(text_values)}) and generated embeddings ({len(embedding_result.embeddings)}) for email {email_db_id}.")
        raise RuntimeError("Embedding generation returned unexpected number of results.")

    # --- 7. Store Embeddings ---
    embedding_model_name = embedding_result.model_name
    for i, text_key in enumerate(text_keys):
        original_text = texts_to_embed[text_key]
        embedding_vector = embedding_result.embeddings[i]
        embedding_type = embedding_types[text_key]
        # Determine chunk index (0 for title/summary, 1+ for content)
        chunk_index = 0
        if embedding_type == "content_chunk":
            try:
                # Extract index from key like "content_chunk_0" -> 0, adjust to be 1-based?
                # Let's keep 0-based for simplicity matching the key, but design doc uses 1+
                # Sticking to 0 for now based on key "content_chunk_0"
                chunk_index = int(text_key.split('_')[-1])
            except (IndexError, ValueError):
                 logger.warning(f"Could not parse chunk index from key '{text_key}', defaulting to 0.")
                 chunk_index = 0 # Fallback

        logger.debug(f"Adding embedding: doc={vector_doc_id}, chunk={chunk_index}, type={embedding_type}, model={embedding_model_name}")
        await storage.add_embedding(
            db_context=db_context,
            document_id=vector_doc_id,
            chunk_index=chunk_index,
            embedding_type=embedding_type,
            embedding=embedding_vector,
            embedding_model=embedding_model_name,
            content=original_text,
            # content_hash=None # Optional: calculate hash if needed
        )

    logger.info(f"Successfully stored {len(embedding_result.embeddings)} embeddings for email {email_db_id} (Vector Doc ID: {vector_doc_id}).")
    # Task completion is handled by the worker loop


# --- Dependency Injection ---
def set_indexing_dependencies(
    embedding_generator: EmbeddingGenerator,
    llm_client: Optional[LLMInterface] = None
):
    """Sets the necessary dependencies for the email indexer."""
    global embedding_generator_instance, llm_client_instance
    embedding_generator_instance = embedding_generator
    llm_client_instance = llm_client
    logger.info("Indexing dependencies set (EmbeddingGenerator, LLMClient).")


__all__ = ["EmailDocument", "handle_index_email", "set_indexing_dependencies"]
