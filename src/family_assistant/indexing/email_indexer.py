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

from family_assistant.config_models import AppConfig
from family_assistant.email_intake.taint import email_provenance_metadata
from family_assistant.indexing.pipeline import IndexableContent, IndexingPipeline
from family_assistant.indexing.types import (
    EmailAttachmentInfo,
    EmailMetadata,
    IndexableContentMetadata,
)
from family_assistant.services.attachment_registry import (
    AttachmentRegistry,
    compute_email_identity_hash,
)
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import (
    AttachmentData,
    parse_attachment_infos_with_raw,
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
    def from_row(
        cls,
        row: Mapping[str, Any],
        *,
        provenance_metadata: dict[str, object] | None = None,
    ) -> "EmailDocument":
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
        if provenance_metadata is not None:
            base_metadata.update(cast("EmailMetadata", provenance_metadata))

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
    provenance_metadata: dict[str, object],
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
    # Dedup on the bounded identity hash (see
    # ``uix_attachment_metadata_email_identity``). Using the raw
    # source_id/storage_path columns would risk the Postgres btree index
    # row-size limit for long Message-Id headers or paths.
    identity_hash = compute_email_identity_hash(
        message_id_header, attachment.storage_path
    )

    # Fast-path: existing row (no transient conflict needed).
    existing_row = await db_context.fetch_one(
        select(attachment_metadata_table.c.attachment_id)
        .where(attachment_metadata_table.c.source_type == "email")
        .where(attachment_metadata_table.c.email_identity_hash == identity_hash)
        .limit(1)
    )
    if existing_row:
        return existing_row["attachment_id"]

    new_id = str(uuid.uuid4())
    conn = db_context.conn
    if conn is None:
        # Hard-fail: silently returning None would surface as a perpetual
        # "needs reindex" state to readers while the real problem is a
        # missing DB connection.
        raise RuntimeError(
            f"Cannot register email attachment '{attachment.filename}' for "
            f"message {message_id_header}: no active database connection"
        )

    # Wrap the insert in a savepoint so that on Postgres a unique-violation
    # does not abort the outer indexer transaction. Once the savepoint is
    # rolled back we can safely re-query for the canonical row.
    savepoint = await conn.begin_nested()
    try:
        # Email indexing is ambient (ownerless attachment)
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
                **provenance_metadata,
            },
        )
    except IntegrityError:
        await savepoint.rollback()
        logger.info(
            "Email attachment %s for message %s was registered concurrently; "
            "reusing existing registry row.",
            attachment.filename,
            message_id_header,
        )
        winner = await db_context.fetch_one(
            select(attachment_metadata_table.c.attachment_id)
            .where(attachment_metadata_table.c.source_type == "email")
            .where(attachment_metadata_table.c.email_identity_hash == identity_hash)
            .limit(1)
        )
        return winner["attachment_id"] if winner else None
    except Exception:
        # Roll back the savepoint so the outer indexer transaction stays
        # usable, then re-raise. We deliberately do NOT swallow the error
        # here: silently returning ``None`` would surface as
        # ``attachment_id=null`` to readers and hide the real problem
        # behind a perpetual "needs reindex" hint. Letting the task fail
        # makes the failure visible in the task queue.
        await savepoint.rollback()
        raise
    await savepoint.commit()
    return new_id


async def _register_resolvable_email_attachment(
    db_context: DatabaseContext,
    attachment_registry: AttachmentRegistry,
    email_db_id: int,
    message_id_header: str,
    attachment: AttachmentData,
    provenance_metadata: dict[str, object],
) -> str | None:
    """Register a single email attachment whose file has been verified on disk.

    Called from the per-attachment loop in
    :meth:`EmailIndexer.handle_index_email` *after* the storage path has been
    resolved to an existing file, so we never persist an ``attachment_id`` that
    immediately 404s when a client tries to download it.

    Returns the canonical ``attachment_id`` (new or previously-registered), or
    ``None`` if a concurrent-registration race left no discoverable canonical
    row.
    """
    return await _register_or_reuse_email_attachment(
        db_context=db_context,
        attachment_registry=attachment_registry,
        email_db_id=email_db_id,
        message_id_header=message_id_header,
        attachment=attachment,
        provenance_metadata=provenance_metadata,
    )


# --- EmailIndexer Class ---
class EmailIndexer:
    """
    Handles the indexing process for emails stored in the database.
    """

    def __init__(
        self,
        pipeline: IndexingPipeline,
        attachment_registry: AttachmentRegistry,
        app_config: AppConfig | None = None,
    ) -> None:
        """Initialize the EmailIndexer.

        Args:
            pipeline: The IndexingPipeline instance to use for processing
                emails.
            attachment_registry: Registry used to register email
                attachments and resolve their on-disk paths. Required —
                the indexer always needs it to register attachment ids
                and to locate files under the configured mailbox base
                path.
        """
        self.pipeline = pipeline
        self.attachment_registry = attachment_registry
        self.app_config = app_config or AppConfig()
        logger.info("EmailIndexer initialized with an IndexingPipeline instance.")

    def _resolve_email_attachment_path(
        self,
        att: AttachmentData,
        email_db_id: int,
    ) -> str | None:
        """Resolve ``att.storage_path`` to an absolute path that exists on disk.

        Delegates to the registry, which knows
        ``email_attachment_base_path`` and checks file existence. Logs and
        returns ``None`` for missing or unresolvable files — we never pass
        a cwd-relative or missing path through to the pipeline.
        """
        if not att.storage_path:
            return None
        resolved = self.attachment_registry.get_attachment_path(
            att.attachment_id or "unused",
            stored_path=att.storage_path,
            source_type="email",
        )
        if resolved is None:
            logger.warning(
                "Email attachment %s for email %s could not be located "
                "on disk (stored_path=%s); skipping attachment extraction.",
                att.filename,
                email_db_id,
                att.storage_path,
            )
            return None
        return str(resolved)

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
        provenance_metadata = email_provenance_metadata(
            email_db_id=email_db_id,
            email_row=email_row,
            app_config=self.app_config,
        )
        try:
            email_doc = EmailDocument.from_row(
                email_row,
                provenance_metadata=provenance_metadata,
            )
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
            body_metadata = {
                "original_source": "email_body",
                **provenance_metadata,
            }
            # The pipeline will handle title extraction, chunking, summarizing, etc.
            # Provide the raw plain text body.
            plain_text_item = IndexableContent(
                content=email_doc.content_plain,
                embedding_type="raw_body_text",
                mime_type="text/plain",
                source_processor="EmailIndexer.handle_index_email",
                metadata=cast("IndexableContentMetadata", body_metadata),
            )
            initial_items.append(plain_text_item)

        # Add attachments to the pipeline
        if email_doc.attachments:
            attachment_registry = self.attachment_registry
            # Validate per-entry so one malformed legacy record doesn't
            # abort indexing of the rest of the email. Keep the raw
            # payload alongside each parsed model so the write-back
            # step at the end of the loop can preserve entries we
            # skipped instead of silently dropping them from the
            # persisted JSON.
            attachment_pairs = parse_attachment_infos_with_raw(
                list(email_doc.attachments),
                context=f"email_db_id={email_db_id}",
            )
            # ``new_attachment_info`` starts as the raw entries and is
            # only overwritten where we mutate the parsed model. That
            # way malformed entries round-trip untouched on write-back.
            new_attachment_info: list[Any] = [raw for raw, _ in attachment_pairs]

            # Each attachment's chunks share the same parent document_id with
            # the email body and each other, so allocate a disjoint
            # chunk_index slot to every attachment (and reserve 0..spacing-1
            # for the email body). The TextChunker honors
            # ``chunk_index_offset`` from item.metadata.
            chunk_index_spacing = 1_000_000
            attachment_info_dirty = False
            for attachment_index, (_raw, att) in enumerate(attachment_pairs):
                if att is None:
                    # Malformed entry already logged by parse_attachment_infos_with_raw.
                    # Keep the raw payload in new_attachment_info and skip.
                    continue
                if not att.storage_path or not att.content_type:
                    logger.warning(
                        f"Skipping attachment for email {email_db_id} due to missing path or mime_type: {att}"
                    )
                    continue

                # Resolve ``att.storage_path`` (stored relative to the
                # configured mailbox base for portability) to a concrete
                # absolute path that exists on disk BEFORE registering the
                # attachment in the registry. Registering first and then
                # discovering the file is gone would leave an
                # ``attachment_id`` stored in ``received_emails.attachment_info``
                # that 404s on every subsequent download — registering after
                # path resolution keeps ``attachment_id``/file presence in
                # lockstep.
                resolved_path = self._resolve_email_attachment_path(att, email_db_id)
                if resolved_path is None:
                    # File is gone. If we previously registered an
                    # ``attachment_id`` for it, clear it from the email
                    # row so ``get_full_document_content`` stops surfacing
                    # a stale handle that would 404 on every
                    # ``read_text_attachment`` / ``/api/attachments/{id}``
                    # lookup. The registry row itself is left alone — it
                    # may still be referenced elsewhere and orphan cleanup
                    # is handled separately.
                    if att.attachment_id is not None:
                        logger.warning(
                            "Clearing stale attachment_id %s for email %s "
                            "attachment '%s' — the backing file is no "
                            "longer on disk.",
                            att.attachment_id,
                            email_db_id,
                            att.filename,
                        )
                        att.attachment_id = None
                        new_attachment_info[attachment_index] = att.model_dump()
                        attachment_info_dirty = True
                    continue

                # If the email row already carries an ``attachment_id``,
                # verify the registry row still exists before trusting
                # it: a prior delete (or partial write-back failure) can
                # leave the email pointing at a dangling id that
                # ``read_text_attachment`` / ``/api/attachments/{id}``
                # would 404 forever. Clear dangling ids so the
                # ``attachment_id is None`` branch below re-registers
                # the attachment on this same pass.
                if att.attachment_id is not None:
                    # Email indexing is ambient (no acting user)
                    existing_metadata = await attachment_registry.get_attachment(
                        db_context, att.attachment_id, acting_user_id=None
                    )
                    if existing_metadata is None:
                        logger.warning(
                            "Clearing stale attachment_id %s for email %s "
                            "attachment '%s' — no matching "
                            "attachment_metadata row; will re-register.",
                            att.attachment_id,
                            email_db_id,
                            att.filename,
                        )
                        att.attachment_id = None
                        # Write the cleared value back to the slot now
                        # (the re-register below will overwrite it again
                        # on success; if re-registration fails, the row
                        # still ends up with the stale id removed rather
                        # than persisted).
                        new_attachment_info[attachment_index] = att.model_dump()
                        attachment_info_dirty = True

                # Now that the file is confirmed on disk and any stale
                # id is cleared, register (or reuse) the registry row
                # and persist the canonical ``attachment_id`` back to
                # ``received_emails.attachment_info``.
                if att.attachment_id is None:
                    resolved_id = await _register_resolvable_email_attachment(
                        db_context=db_context,
                        attachment_registry=attachment_registry,
                        email_db_id=email_db_id,
                        message_id_header=email_doc.source_id,
                        attachment=att,
                        provenance_metadata=provenance_metadata,
                    )
                    if resolved_id is None:
                        # Concurrent registration race left no discoverable
                        # canonical row — log and continue without an id;
                        # the next reindex will try again.
                        logger.warning(
                            "Could not resolve canonical attachment_id for "
                            "email %s attachment '%s' after concurrent "
                            "registration race; leaving attachment_id unset.",
                            email_doc.source_id,
                            att.filename,
                        )
                    else:
                        att.attachment_id = resolved_id
                        new_attachment_info[attachment_index] = att.model_dump()
                        attachment_info_dirty = True

                logger.info(
                    f"Preparing attachment for pipeline: {att.filename} "
                    f"({att.content_type}) at {resolved_path} "
                    f"(stored: {att.storage_path}) for email {email_db_id}"
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
                            "chunk_index_offset": (attachment_index + 1)
                            * chunk_index_spacing,
                            **provenance_metadata,
                        },
                    ),
                    ref=resolved_path,
                )
                initial_items.append(attachment_item)

            if attachment_info_dirty:
                await db_context.execute_with_retry(
                    update(received_emails_table)
                    .where(received_emails_table.c.id == email_db_id)
                    .values(attachment_info=new_attachment_info)
                )

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
