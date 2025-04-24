"""
Handles storage and retrieval of received emails.
"""

import logging
import os
import re
import os
import re
import json
from typing import Any, Dict, Optional
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.sql import insert # Explicit import
from sqlalchemy.dialects.postgresql import JSONB
import json
from dateutil.parser import parse as parse_datetime

# Import metadata and engine from the main storage module
from db_base import metadata, get_engine

logger = logging.getLogger(__name__)
# Import metadata and engine from the base module
engine = get_engine()
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
        server_default=sa.func.now(),
        nullable=False,
        index=True,
    ),  # Timestamp when the webhook was received
    sa.Column(
        "email_date", sa.DateTime(timezone=True), nullable=True, index=True
    ),  # Timestamp from the email's 'Date' header
    sa.Column(
        "headers_json", JSONB, nullable=True
    ),  # All headers stored as JSON (from message-headers)
    sa.Column(
        "attachment_info", JSONB, nullable=True
    ),  # JSON array [{filename, content_type, size, storage_path}, ...]
    # Add other potentially useful Mailgun fields if needed
    sa.Column("mailgun_timestamp", sa.Text, nullable=True), # Mailgun 'timestamp' field
    sa.Column("mailgun_token", sa.Text, nullable=True), # Mailgun 'token' field
)


async def store_incoming_email(form_data: Dict[str, Any]):
    """
    Parses incoming email data (from Mailgun webhook form) and prepares it for storage.
    Stores the parsed data in the `received_emails` table.

    Args:
        form_data: A dictionary representing the form data received from the webhook.
    """
    logger.info("Parsing incoming email data for storage...")

    email_date_parsed: Optional[datetime] = None
    email_date_str = form_data.get("Date")
    if email_date_str:
        try:
            email_date_parsed = parse_datetime(email_date_str)
            # Ensure timezone-aware
            if email_date_parsed.tzinfo is None:
                # Assuming UTC if timezone is missing, adjust if needed based on common sources
                email_date_parsed = email_date_parsed.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.warning(
                f"Could not parse email Date header '{email_date_str}': {e}"
            )

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
        "cc_header": form_data.get("Cc"), # May not be present
        "subject": form_data.get("subject"),
        "body_plain": form_data.get("body-plain"),
        "body_html": form_data.get("body-html"),
        "stripped_text": form_data.get("stripped-text"),
        "stripped_html": form_data.get("stripped-html"),
        "email_date": email_date_parsed,
        "headers_json": headers_list,
        "attachment_info": None, # Placeholder
        "mailgun_timestamp": form_data.get("timestamp"),
        "mailgun_token": form_data.get("token"),
    }
    logger.info(f"Parsed email data for storage: {parsed_data}")

    # --- Actual Database Insertion ---
    engine = get_engine()
        stmt = insert(received_emails_table).values(**parsed_data) # Use explicit insert
        await conn.execute(stmt)
        await conn.commit()
        logger.info(f"Stored email with Message-ID: {parsed_data['message_id_header']}") # noqa: E501

# Export symbols for use elsewhere
__all__ = ["received_emails_table", "store_incoming_email"]
