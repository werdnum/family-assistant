"""
Handles the indexing process for emails stored in the database.
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypedDict, cast

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant.indexing.pipeline import IndexableContent, IndexingPipeline
from family_assistant.indexing.types import (
    EmailAttachmentInfo,
    EmailMetadata,
    IndexableContentMetadata,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import (
    AttachmentData,
    received_emails_table,
)
from family_assistant.storage.vector import Document, get_document_by_id
from family_assistant.tools import ToolExecutionContext


class EmailIndexPayload(TypedDict):
    """Payload for email indexing tasks."""

    email_db_id: int


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
    _title: str | None = None
    _created_at: datetime | None = None
    _source_uri: str | None = None
    _base_metadata: EmailMetadata = field(
        default_factory=lambda: cast("EmailMetadata", {})
    )
    _content_plain: str | None = None
    _attachment_info_raw: list[EmailAttachmentInfo] | None = None

    @property
    def source_type(self) -> str:
        """The type of the source ('email')."""
        return "email"

    @property
    def source_id(self) -> str:
        """The unique identifier (Message-ID header)."""
        return self._source_id

    @property
    def source_uri(self) -> str | None:
        """URI or path to the original item (not typically available for emails)."""
        return self._source_uri  # Could potentially be a mail archive link if available

    @property
    def title(self) -> str | None:
        """Title or subject of the document."""
        return self._title

    @property
    def created_at(self) -> datetime | None:
        """Original creation date (from 'Date' header, timezone-aware)."""
        return self._created_at

    @property
    def metadata(self) -> EmailMetadata | None:
        """Base metadata extracted directly from the source."""
        return self._base_metadata

    @property
    def content_plain(self) -> str | None:
        """The plain text content of the email (e.g., stripped_text)."""
        return self._content_plain

    @property
    def attachments(self) -> list[EmailAttachmentInfo] | None:
        """List of attachment metadata dictionaries."""
        return self._attachment_info_raw

    @property
    def file_path(self) -> str | None:
        return None

    @property
    def visibility_labels(self) -> list[str] | None:
        return None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EmailDocument":
        """
        Creates an EmailDocument instance from a SQLAlchemy RowMapping (or compatible mapping)
        representing a row from the received_emails table.
        """
        # Ensure required fields are present
        message_id = row.get("message_id_header")
        if not message_id:
            raise ValueError(
                "Cannot create EmailDocument: 'message_id_header' is missing from row."
            )

        # Extract base metadata
        base_metadata: EmailMetadata = {}
        for key in [
            "sender_address",
            "from_header",
            "recipient_address",
            "to_header",
            "cc_header",
            "mailgun_timestamp",
        ]:
            if (value := row.get(key)) is not None:
                base_metadata[key] = value  # type: ignore
        if (headers_json := row.get("headers_json")) is not None:
            base_metadata["headers"] = headers_json

        # Prefer stripped_text for cleaner content
        content = row.get("stripped_text") or row.get("body_plain")
        attachment_info_data = row.get(
            "attachment_info"
        )  # This is a list of dicts or None

        return cls(
            _source_id=message_id,
            _title=row.get("subject"),
            _created_at=row.get("email_date"),  # Already parsed to datetime or None
            _base_metadata=base_metadata,
            _content_plain=content,
            _attachment_info_raw=attachment_info_data,
            # _source_uri could be set if a web view link exists, otherwise None
        )

    def to_dict(self) -> dict[str, str | EmailMetadata | None]:
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


async def _register_or_reuse_email_attachment(
    *,
    db_context: DatabaseContext,
    attachment_registry: AttachmentRegistry,
    email_db_id: int,
    message_id_header: str,
    attachment: AttachmentData,
) -> str | None:
    """Register an email attachment or return the canonical id if one already
    exists for ``(source_type="email", source_id, storage_path)``.

    The partial unique index ``uix_attachment_metadata_email_identity`` makes
    the insert atomic: if two concurrent indexer runs race for the same email
    attachment, only one INSERT succeeds and the other raises
    ``IntegrityError``. In both branches we finish with the canonical row's
    ``attachment_id``.

    Returns ``None`` if registration failed for a reason other than a
    uniqueness conflict.
    """
    # Fast-path: existing row (no transient conflict needed).
    existing_row = await db_context.fetch_one(
        select(attachment_metadata_table.c.attachment_id)
        .where(attachment_metadata_table.c.source_type == "email")
        .where(attachment_metadata_table.c.source_id == message_id_header)
        .where(attachment_metadata_table.c.storage_path == attachment.storage_path)
        .limit(1)
    )
    if existing_row:
        return existing_row["attachment_id"]

    new_id = str(uuid.uuid4())
    try:
        await attachment_registry.register_attachment(
            db_context=db_context,
            attachment_id=new_id,
            source_type="email",
            source_id=message_id_header,
            mime_type=attachment.content_type,
            description=f"Email attachment: {attachment.filename}",
            size=attachment.size or 0,
            storage_path=attachment.storage_path,
            content_url=f"/api/attachments/{new_id}",
            metadata={
                "original_filename": attachment.filename,
                "email_message_id": message_id_header,
                "email_db_id": email_db_id,
            },
        )
    except IntegrityError:
        # Another worker inserted the canonical row between our lookup and
        # our insert. Re-query to pick up the winner's id.
        logger.info(
            "Email attachment %s for message %s was registered concurrently; "
            "reusing existing registry row.",
            attachment.filename,
            message_id_header,
        )
        winner = await db_context.fetch_one(
            select(attachment_metadata_table.c.attachment_id)
            .where(attachment_metadata_table.c.source_type == "email")
            .where(attachment_metadata_table.c.source_id == message_id_header)
            .where(attachment_metadata_table.c.storage_path == attachment.storage_path)
            .limit(1)
        )
        return winner["attachment_id"] if winner else None
    except Exception as reg_err:
        logger.error(
            "Failed to register email attachment '%s' for message %s: %s",
            attachment.filename,
            message_id_header,
            reg_err,
            exc_info=True,
        )
        return None
    return new_id


async def _ensure_email_attachments_registered(
    db_context: DatabaseContext,
    attachment_registry: AttachmentRegistry,
    email_db_id: int,
    message_id_header: str,
    raw_attachment_info: list[Mapping[str, Any]],
) -> list[AttachmentData]:
    """Register any email attachments that do not yet have an ``attachment_id``.

    Runs on the indexing write path where each ``email_db_id`` is handled by a
    single task, so there is no concurrent writer for the same email row. The
    function returns the updated attachment list as ``AttachmentData`` models
    and, if any IDs were added, persists the update to
    ``received_emails.attachment_info``.
    """
    attachments = [AttachmentData.model_validate(item) for item in raw_attachment_info]
    updated = False

    for att in attachments:
        if att.attachment_id:
            continue

        resolved_id = await _register_or_reuse_email_attachment(
            db_context=db_context,
            attachment_registry=attachment_registry,
            email_db_id=email_db_id,
            message_id_header=message_id_header,
            attachment=att,
        )
        if resolved_id is None:
            continue
        att.attachment_id = resolved_id
        updated = True

    if updated:
        await db_context.execute_with_retry(
            update(received_emails_table)
            .where(received_emails_table.c.id == email_db_id)
            .values(attachment_info=[att.model_dump() for att in attachments])
        )

    return attachments


# --- EmailIndexer Class ---
class EmailIndexer:
    """
    Handles the indexing process for emails stored in the database.
    """

    def __init__(self, pipeline: IndexingPipeline) -> None:
        """
        Initializes the EmailIndexer.

        Args:
            pipeline: The IndexingPipeline instance to use for processing emails.
        """
        self.pipeline = pipeline
        logger.info("EmailIndexer initialized with an IndexingPipeline instance.")

    async def handle_index_email(
        self,
        exec_context: ToolExecutionContext,
        payload: EmailIndexPayload,
    ) -> None:
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

        if not self.pipeline:  # Should always be set by constructor
            raise RuntimeError(
                "IndexingPipeline dependency not set for email indexing."
            )

        logger.info(f"Starting indexing for email DB ID: {email_db_id}")

        # --- 1. Fetch Email Data ---
        # No need to update status here, task status handles it
        select_stmt = select(received_emails_table).where(
            received_emails_table.c.id == email_db_id
        )
        email_row = await db_context.fetch_one(select_stmt)

        if not email_row:
            # Email might have been deleted between enqueueing and processing
            logger.warning(
                f"Email {email_db_id} not found in database. Skipping indexing."
            )
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
        doc_db_id: int = await db_context.vector.add_document(
            doc=email_doc,
            enriched_doc_metadata=enriched_metadata,
        )
        logger.info(
            f"Added/Updated document record for email {email_db_id}, vector DB doc ID: {doc_db_id}"
        )

        try:
            db_document_record = await get_document_by_id(db_context, doc_db_id)
            if not db_document_record:
                # This should ideally not happen if add_document succeeded
                raise ValueError(
                    f"Failed to retrieve document record for ID {doc_db_id} after adding/updating."
                )
        except SQLAlchemyError as e:
            logger.error(
                f"Database error fetching document record {doc_db_id}: {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Failed to fetch document record {doc_db_id}") from e

        # --- 5. Prepare Initial Content for Pipeline ---
        initial_items: list[IndexableContent] = []
        if email_doc.content_plain:
            # The pipeline will handle title extraction, chunking, summarizing, etc.
            # Provide the raw plain text body.
            plain_text_item = IndexableContent(
                content=email_doc.content_plain,
                embedding_type="raw_body_text",
                mime_type="text/plain",
                source_processor="EmailIndexer.handle_index_email",
                metadata=cast(
                    "IndexableContentMetadata", {"original_source": "email_body"}
                ),
            )
            initial_items.append(plain_text_item)

        # Add attachments to the pipeline
        if email_doc.attachments:
            attachment_registry = exec_context.attachment_registry
            if attachment_registry is not None:
                registered_attachments = await _ensure_email_attachments_registered(
                    db_context=db_context,
                    attachment_registry=attachment_registry,
                    email_db_id=email_db_id,
                    message_id_header=email_doc.source_id,
                    raw_attachment_info=list(email_doc.attachments),
                )
            else:
                logger.info(
                    "AttachmentRegistry not available during indexing of email "
                    f"{email_db_id}; skipping attachment registration."
                )
                registered_attachments = [
                    AttachmentData.model_validate(item)
                    for item in email_doc.attachments
                ]

            for att in registered_attachments:
                if not att.storage_path or not att.content_type:
                    logger.warning(
                        f"Skipping attachment for email {email_db_id} due to missing path or mime_type: {att}"
                    )
                    continue

                logger.info(
                    f"Preparing attachment for pipeline: {att.filename} "
                    f"({att.content_type}) at {att.storage_path} for email {email_db_id}"
                )
                attachment_item = IndexableContent(
                    content=None,
                    embedding_type="email_attachment_file",
                    mime_type=att.content_type,
                    source_processor="EmailIndexer.handle_index_email.attachment",
                    metadata=cast(
                        "IndexableContentMetadata",
                        {
                            "original_filename": att.filename,
                            "email_db_id": email_db_id,
                            "email_source_id": email_doc.source_id,
                            "attachment_id": att.attachment_id,
                        },
                    ),
                    ref=att.storage_path,
                )
                initial_items.append(attachment_item)

        if not initial_items:
            logger.warning(
                f"No text content or attachments found to pass to pipeline for email {email_db_id}. Skipping pipeline run."
            )
            # Task is considered done as the document record was created/updated.
            return

        # --- 6. Run Indexing Pipeline ---
        try:
            logger.info(
                f"Running indexing pipeline for email {email_db_id} (Doc ID: {doc_db_id}) with {len(initial_items)} initial items."
            )
            await self.pipeline.run(
                initial_items=initial_items,
                original_document=cast(
                    "Document", db_document_record
                ),  # Pass the DB record, cast for protocol
                context=exec_context,
            )
        except Exception as e:
            logger.error(
                f"Indexing pipeline run failed for email {email_db_id} (Doc ID: {doc_db_id}): {e}",
                exc_info=True,
            )
            raise RuntimeError(
                f"Indexing pipeline failed for email {email_db_id}"
            ) from e

        logger.info(
            f"Indexing pipeline successfully initiated for email {email_db_id} (Doc ID: {doc_db_id})."
        )
        # Task completion is handled by the worker loop


__all__ = ["EmailDocument", "EmailIndexer"]
