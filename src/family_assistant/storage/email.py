"""
Handles storage and retrieval of received emails.
"""

import logging
import uuid  # Add uuid import
from datetime import datetime  # Added for Pydantic models

import sqlalchemy as sa
from pydantic import BaseModel, Field  # Added for Pydantic models
from sqlalchemy import JSON  # Import generic JSON type
from sqlalchemy.dialects.postgresql import JSONB  # Import PostgreSQL specific JSONB
from sqlalchemy.exc import SQLAlchemyError  # Use broader exception
from sqlalchemy.sql import functions, insert, update  # Consolidate and add update

# Import storage facade for enqueue_task
# Import metadata and engine using absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext


# --- Pydantic Models for Parsed Email Data ---
class AttachmentData(BaseModel):
    """Represents metadata for a single email attachment."""

    filename: str
    content_type: str
    size: int | None = None
    storage_path: str  # Path where the attachment is saved


class ParsedEmailData(BaseModel):
    """Represents parsed data from an incoming email webhook."""

    message_id_header: str = Field(..., alias="Message-Id")
    sender_address: str | None = Field(default=None, alias="sender")
    from_header: str | None = Field(default=None, alias="From")
    recipient_address: str | None = Field(default=None, alias="recipient")
    to_header: str | None = Field(default=None, alias="To")
    cc_header: str | None = Field(default=None, alias="Cc")
    subject: str | None = Field(default=None, alias="subject")
    body_plain: str | None = Field(default=None, alias="body-plain")
    body_html: str | None = Field(default=None, alias="body-html")
    stripped_text: str | None = Field(default=None, alias="stripped-text")
    stripped_html: str | None = Field(default=None, alias="stripped-html")
    email_date: datetime | None = None  # Parsed by webhook handler
    headers_json: list[list[str]] | None = None  # Parsed by webhook handler
    attachment_info: list[AttachmentData] | None = None  # Processed by webhook handler
    mailgun_timestamp: str | None = Field(default=None, alias="timestamp")
    mailgun_token: str | None = Field(default=None, alias="token")

    class Config:
        allow_population_by_field_name = True


logger = logging.getLogger(__name__)
# Remove engine = get_engine()
# Define the received emails table
received_emails_table = sa.Table(
    "received_emails",
    metadata,
    sa.Column(
        "id", sa.BigInteger, primary_key=True, autoincrement=True
    ),  # Internal primary key
    sa.Column(
        "message_id_header", sa.Text, nullable=False, unique=True, index=True
    ),  # Message-ID header, unique identifier
    sa.Column(
        "sender_address", sa.Text, nullable=True, index=True
    ),  # Mailgun 'sender' field (envelope from)
    sa.Column("from_header", sa.Text, nullable=True),  # 'From' header content
    sa.Column(
        "recipient_address", sa.Text, nullable=True, index=True
    ),  # Mailgun 'recipient' field (envelope to)
    sa.Column("to_header", sa.Text, nullable=True),  # 'To' header content
    sa.Column("cc_header", sa.Text, nullable=True),  # 'Cc' header content
    sa.Column("subject", sa.Text, nullable=True),  # Email subject
    sa.Column("body_plain", sa.Text, nullable=True),  # Raw plain text body
    sa.Column("body_html", sa.Text, nullable=True),  # Raw HTML body
    sa.Column(
        "stripped_text", sa.Text, nullable=True
    ),  # Mailgun stripped plain text body
    sa.Column(
        "stripped_html", sa.Text, nullable=True
    ),  # Mailgun stripped HTML body (without signature)
    sa.Column(
        "received_at",
        sa.DateTime(timezone=True),
        server_default=functions.now(),  # Use explicit import
        nullable=False,
        index=True,
    ),  # Timestamp when the webhook was received
    sa.Column(
        "email_date", sa.DateTime(timezone=True), nullable=True, index=True
    ),  # Timestamp from the email's 'Date' header
    sa.Column(
        "headers_json", JSON().with_variant(JSONB, "postgresql"), nullable=True
    ),  # Use JSONB for Postgres, JSON otherwise
    sa.Column(
        "attachment_info", JSON().with_variant(JSONB, "postgresql"), nullable=True
    ),  # Use JSONB for Postgres, JSON otherwise
    # Add other potentially useful Mailgun fields if needed
    sa.Column("mailgun_timestamp", sa.Text, nullable=True),  # Mailgun 'timestamp' field
    sa.Column("mailgun_token", sa.Text, nullable=True),  # Mailgun 'token' field
    # --- Indexing Task Tracking ---
    sa.Column(
        "indexing_task_id", sa.String, nullable=True, index=True, unique=True
    ),  # Stores the unique ID of the task responsible for indexing this email
)


# --- Database Operations ---


async def store_incoming_email(
    db_context: DatabaseContext,
    parsed_email: ParsedEmailData,  # Changed from form_data
) -> None:
    """
    Stores parsed email data in the `received_emails` table and enqueues an indexing task.

    Args:
        db_context: The DatabaseContext to use for the operation.
        parsed_email: A Pydantic model instance containing the parsed email data.
        notify_event: An optional asyncio.Event to notify upon task enqueueing.
    """
    logger.info(
        f"Storing parsed email data for Message-ID: {parsed_email.message_id_header}"
    )

    # Convert Pydantic model to dict for database insertion.
    # Use `exclude_unset=True` if you only want to insert fields that were explicitly set,
    # or `exclude_none=True` to avoid inserting None values for nullable fields if DB handles defaults.
    # Here, we'll use by_alias=True to ensure DB columns match model field names if they differ.
    email_data_for_db = parsed_email.model_dump(
        by_alias=False, exclude_none=True
    )  # Use model_dump for Pydantic v2

    # Ensure message_id_header is present (it's non-nullable in Pydantic model and DB)
    if not email_data_for_db.get("message_id_header"):
        # This should ideally be caught by Pydantic validation if alias "Message-Id" is not found
        logger.error(
            "Cannot store email: 'message_id_header' (aliased as 'Message-Id') is missing after Pydantic parsing."
        )
        raise ValueError(
            "Cannot store email: 'message_id_header' is missing after Pydantic parsing."
        )

    # attachment_info needs to be JSON serializable if it's a list of Pydantic models
    if (
        "attachment_info" in email_data_for_db
        and email_data_for_db["attachment_info"] is not None
    ):
        email_data_for_db["attachment_info"] = [
            att.model_dump()
            for att in parsed_email.attachment_info  # type: ignore
        ]

    logger.debug(f"Attempting to store email data: {email_data_for_db}")

    # --- Actual Database Insertion and Task Enqueueing ---
    email_db_id: int | None = None
    task_id: str | None = None
    try:
        # 1. Insert email and get its ID
        insert_stmt = (
            insert(received_emails_table)
            .values(**email_data_for_db)
            .returning(received_emails_table.c.id)
        )
        result = await db_context.execute_with_retry(insert_stmt)
        email_db_id = result.scalar_one_or_none()

        if not email_db_id:
            raise RuntimeError(
                f"Failed to retrieve DB ID after inserting email with Message-ID: {parsed_email.message_id_header}"
            )

        logger.info(
            f"Stored email with Message-ID: {parsed_email.message_id_header}, DB ID: {email_db_id}"
        )

        # 2. Generate a unique task ID
        task_id = f"index_email_{email_db_id}_{uuid.uuid4()}"

        # 3. Enqueue the indexing task
        await db_context.tasks.enqueue(
            task_id=task_id,
            task_type="index_email",
            payload={"email_db_id": email_db_id},
        )
        logger.info(f"Enqueued indexing task {task_id} for email DB ID {email_db_id}")

        # 4. Update the email record with the task ID
        update_stmt = (
            update(received_emails_table)
            .where(received_emails_table.c.id == email_db_id)
            .values(indexing_task_id=task_id)
        )
        await db_context.execute_with_retry(update_stmt)
        logger.info(f"Updated email {email_db_id} with indexing task ID {task_id}")

    except SQLAlchemyError as e:
        failed_stage = "inserting email"
        if email_db_id and not task_id:
            failed_stage = "enqueueing task"
        elif email_db_id and task_id:
            failed_stage = "updating email with task_id"

        logger.error(
            f"Database error during {failed_stage} for email Message-ID {parsed_email.message_id_header} (DB ID: {email_db_id}, Task ID: {task_id}): {e}",
            exc_info=True,
        )
        raise


# Export symbols for use elsewhere
__all__ = [
    "received_emails_table",
    "store_incoming_email",
    "ParsedEmailData",
    "AttachmentData",
]
