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
from sqlalchemy.sql import insert, functions  # Import functions explicitly
from sqlalchemy.sql import insert
from sqlalchemy import JSON  # Import generic JSON type
from sqlalchemy.dialects.postgresql import JSONB  # Import PostgreSQL specific JSONB
import json
from dataclasses import dataclass, field  # Add dataclass imports
from dateutil.parser import parse as parse_datetime
from sqlalchemy.exc import SQLAlchemyError  # Use broader exception
from sqlalchemy.engine import (
    RowMapping,
)  # Import RowMapping needed for EmailDocument.from_row

# Import metadata and engine using absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext

# Import the Document protocol
from family_assistant.storage.vector import Document

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
)


# --- EmailDocument Class ---


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


# --- Database Operations ---


async def store_incoming_email(db_context: DatabaseContext, form_data: Dict[str, Any]):
    """
    Parses incoming email data (from Mailgun webhook form) and prepares it for storage.
    Stores the parsed data in the `received_emails` table using the provided context.

    Args:
        db_context: The DatabaseContext to use for the operation.
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

    # --- Actual Database Insertion ---
    try:
        stmt = insert(received_emails_table).values(**parsed_data_filtered)
        # Use execute_with_retry as commit is handled by context manager
        await db_context.execute_with_retry(stmt)
        logger.info(
            f"Stored email with Message-ID: {parsed_data_filtered['message_id_header']}"
        )
    except SQLAlchemyError as e:
        logger.error(
            f"Database error storing email with Message-ID {parsed_data_filtered.get('message_id_header', 'N/A')}: {e}",
            exc_info=True,
        )
        raise


# Export symbols for use elsewhere
__all__ = ["received_emails_table", "store_incoming_email", "EmailDocument"]
