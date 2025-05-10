"""
Handles storage and retrieval of received emails.
"""

import asyncio  # Import asyncio for Event type hint

import json
import logging
import uuid  # Add uuid import
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from dateutil.parser import parse as parse_datetime

from sqlalchemy import JSON  # Import generic JSON type
from sqlalchemy.dialects.postgresql import JSONB  # Import PostgreSQL specific JSONB
from sqlalchemy.exc import SQLAlchemyError  # Use broader exception
from sqlalchemy.sql import functions, insert, update  # Consolidate and add update

# Import storage facade for enqueue_task
from family_assistant import storage

# Import metadata and engine using absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext

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
    form_data: dict[str, Any],
    notify_event: asyncio.Event | None = None,  # Add notify_event parameter
) -> None:
    """
    Parses incoming email data (from Mailgun webhook form) and prepares it for storage.
    Stores the parsed data in the `received_emails` table using the provided context,
    optionally notifying a worker event.

    Args:
        db_context: The DatabaseContext to use for the operation.
        form_data: A dictionary representing the form data received from the webhook.
    """
    logger.info("Parsing incoming email data for storage...")

    email_date_parsed: datetime | None = None
    email_date_str = form_data.get("Date")
    if email_date_str:
        try:
            email_date_parsed = parse_datetime(email_date_str)
            # Ensure timezone-aware
            if email_date_parsed.tzinfo is None:
                # Assuming UTC if timezone is missing, adjust if needed based on common sources
                email_date_parsed = email_date_parsed.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.warning(f"Could not parse email Date header '{email_date_str}': {e}")

    # Extract headers (Mailgun sends this as a JSON string representation of list of lists)
    headers_list = None
    headers_raw = form_data.get("message-headers")
    if headers_raw:
        try:
            headers_list = json.loads(headers_raw)
        except json.JSONDecodeError as e:
            logger.warning(f"Could not decode message-headers JSON: {e}")

    # Prepare data for insertion
    parsed_data = {
        "message_id_header": form_data.get("Message-Id"),
        "sender_address": form_data.get("sender"),
        "from_header": form_data.get("From"),
        "recipient_address": form_data.get("recipient"),
        "to_header": form_data.get("To"),
        "cc_header": form_data.get("Cc"),  # May not be present
        "subject": form_data.get("subject"),
        "body_plain": form_data.get("body-plain"),
        "body_html": form_data.get("body-html"),
        "stripped_text": form_data.get("stripped-text"),
        "stripped_html": form_data.get("stripped-html"),
        "email_date": email_date_parsed,
        "headers_json": headers_list,
        "attachment_info": None,  # Placeholder
        "mailgun_timestamp": form_data.get("timestamp"),
        "mailgun_token": form_data.get("token"),
    }
    # Filter out None values before insertion if the column is not nullable
    # (though most are nullable here)
    parsed_data_filtered = {k: v for k, v in parsed_data.items() if v is not None}
    # Ensure message_id_header is present even if None initially (it's nullable=False)
    if (
        "message_id_header" not in parsed_data_filtered
        and "message_id_header" in parsed_data
    ):
        parsed_data_filtered["message_id_header"] = parsed_data["message_id_header"]

    if not parsed_data_filtered.get("message_id_header"):
        logger.error("Cannot store email: Message-ID header is missing.")
        # Decide how to handle this - raise error or just log and return?
        # Raising an error might be better to signal failure.
        raise ValueError("Cannot store email: Message-ID header is missing.")

    logger.debug(f"Attempting to store email data: {parsed_data_filtered}")

    # --- Actual Database Insertion and Task Enqueueing ---
    email_db_id: int | None = None
    task_id: str | None = None
    try:
        # 1. Insert email and get its ID
        insert_stmt = (
            insert(received_emails_table)
            .values(**parsed_data_filtered)
            .returning(received_emails_table.c.id)
        )
        result = await db_context.execute_with_retry(insert_stmt)
        email_db_id = result.scalar_one_or_none()

        if not email_db_id:
            # This shouldn't happen if insert succeeded without error, but check anyway
            raise RuntimeError(
                f"Failed to retrieve DB ID after inserting email with Message-ID: {parsed_data_filtered['message_id_header']}"
            )

        logger.info(
            f"Stored email with Message-ID: {parsed_data_filtered['message_id_header']}, DB ID: {email_db_id}"
        )

        # 2. Generate a unique task ID
        task_id = f"index_email_{email_db_id}_{uuid.uuid4()}"  # Add uuid for potential re-runs

        # 3. Enqueue the indexing task
        await storage.enqueue_task(
            db_context=db_context,
            task_id=task_id,
            task_type="index_email",
            payload={"email_db_id": email_db_id},
            notify_event=notify_event,  # Pass the received event
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
        # Log specific details if available
        failed_stage = "inserting email"
        if email_db_id and not task_id:
            failed_stage = "enqueueing task"
        if email_db_id and task_id:
            failed_stage = "updating email with task_id"

        logger.error(
            f"Database error during {failed_stage} for email Message-ID {parsed_data_filtered.get('message_id_header', 'N/A')} (DB ID: {email_db_id}, Task ID: {task_id}): {e}",
            exc_info=True,
        )
        raise


# Export symbols for use elsewhere
__all__ = ["received_emails_table", "store_incoming_email"]
