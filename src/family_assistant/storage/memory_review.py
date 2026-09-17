"""The durable state the memory review sweep is scheduled from.

See docs/design/conversation-memory.md, "A curator reviews each conversation
when it goes idle". Whether a conversation is due is a pure function of stored
data, so two pieces of it live here rather than in any running process:

- **the watermark**, per ``(interface_type, conversation_id)``, recording the
  last message a review covered. It is what makes the design work on Telegram,
  where a chat id never ends, and what keeps a review of a resumed web
  conversation to the new material.
- **the enablement moment**, per profile, recording when contribution was last
  turned on for it. A review considers only rows newer than that moment, so
  turning the feature on learns from what is said next rather than spending a
  burst of model calls on months of old conversation. One row per profile, not
  a history: eligibility is then a single comparison, and rows written while
  contribution was off are never curated.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
)

from family_assistant.storage.base import metadata

memory_review_watermarks_table = Table(
    "memory_review_watermarks",
    metadata,
    Column("interface_type", String(50), nullable=False),
    Column("conversation_id", String(255), nullable=False),
    # message_history.internal_id of the last row a review covered. Advanced on
    # every terminal outcome -- applied, skipped or abandoned -- so no stretch
    # can block a conversation for good.
    Column("last_reviewed_internal_id", Integer, nullable=False),
    Column("last_reviewed_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint(
        "interface_type", "conversation_id", name="pk_memory_review_watermarks"
    ),
)

memory_contribution_state_table = Table(
    "memory_contribution_state",
    metadata,
    Column("profile_id", String(255), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    # When contribution was last turned on. Kept when it is turned off again,
    # as the record of what the last enabled stretch was; `enabled` is what
    # says whether it still applies.
    Column("enabled_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_memory_contribution_state_enabled", "enabled"),
)
