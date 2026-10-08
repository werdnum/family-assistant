"""Storage table for generated conversation-list summaries."""

from sqlalchemy import Column, DateTime, Integer, String, Table, Text

from family_assistant.storage.base import metadata

conversation_summaries_table = Table(
    "conversation_summaries",
    metadata,
    Column("conversation_id", String(255), primary_key=True),
    # Null when the latest attempt produced nothing usable: the watermark still
    # advances so a conversation that cannot be summarized is not retried on
    # every sweep, and the list falls back to the latest message.
    Column("summary", Text, nullable=True),
    # The highest message_history.internal_id the summary has seen. A newer
    # visible message is what makes the conversation due again.
    Column("summarized_through_id", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
