"""Storage models for proposed actions extracted from inbound email."""

from __future__ import annotations

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import functions

from family_assistant.storage.base import metadata
from family_assistant.storage.email import received_emails_table


class EmailActionProposalData(BaseModel):
    """A pending action proposal derived from an inbound email."""

    email_id: int
    message_id_header: str
    target_user_id: str
    action_type: str
    title: str
    proposal_json: dict[str, object]
    status: str = "proposed"
    rationale: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    safety_warnings: list[str] = Field(default_factory=list)
    planning_task_id: str | None = None

    model_config = ConfigDict(extra="forbid")


email_action_proposals_table = sa.Table(
    "email_action_proposals",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "email_id",
        sa.BigInteger,
        sa.ForeignKey(received_emails_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("message_id_header", sa.Text, nullable=False, index=True),
    sa.Column("target_user_id", sa.Text, nullable=False, index=True),
    sa.Column("action_type", sa.String(64), nullable=False, index=True),
    sa.Column(
        "status", sa.String(32), nullable=False, server_default="proposed", index=True
    ),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("rationale", sa.Text, nullable=True),
    sa.Column("confidence", sa.Float, nullable=True),
    sa.Column(
        "proposal_json", JSON().with_variant(JSONB, "postgresql"), nullable=False
    ),
    sa.Column(
        "safety_warnings", JSON().with_variant(JSONB, "postgresql"), nullable=True
    ),
    sa.Column("planning_task_id", sa.String, nullable=True, index=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=functions.now(),
        nullable=False,
        index=True,
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=functions.now(),
        nullable=False,
    ),
)


__all__ = ["EmailActionProposalData", "email_action_proposals_table"]
