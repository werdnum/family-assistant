"""Shared helpers for registering email attachments in the AttachmentRegistry.

This module hosts the canonical "register one email attachment, or reuse the
existing canonical row" routine used by both the Mailgun webhook (eager
registration, so attachment ids reach the email_intake assistant turn) and
the EmailIndexer (re-runs / reindex). Keeping it in one place ensures both
call sites use the same dedup semantics keyed on
``uix_attachment_metadata_email_identity``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from family_assistant.services.attachment_registry import (
    AttachmentRegistry,
    compute_email_identity_hash,
)
from family_assistant.storage.base import attachment_metadata_table

if TYPE_CHECKING:
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.storage.email import AttachmentData

logger = logging.getLogger(__name__)


async def register_or_reuse_email_attachment(
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
    the insert atomic: if two concurrent ingest paths (webhook + indexer)
    race for the same email attachment, only one INSERT succeeds and the
    other raises ``IntegrityError``. In both branches we finish with the
    canonical row's ``attachment_id``.

    Returns ``None`` if registration failed for a reason other than a
    uniqueness conflict.
    """
    identity_hash = compute_email_identity_hash(
        message_id_header, attachment.storage_path
    )

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
        raise RuntimeError(
            f"Cannot register email attachment '{attachment.filename}' for "
            f"message {message_id_header}: no active database connection"
        )

    savepoint = await conn.begin_nested()
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
        await savepoint.rollback()
        raise
    await savepoint.commit()
    return new_id
