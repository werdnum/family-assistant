"""Push subscription storage models and queries."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Integer, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import functions as func
from sqlalchemy.types import JSON

from family_assistant.storage.base import metadata


# --- Pydantic Model for type-safe data transfer ---
class PushSubscription(BaseModel):
    """Pydantic model for a push subscription."""

    id: int
    subscription_json: dict[str, Any]
    user_identifier: str
    created_at: datetime


# --- SQLAlchemy Core Table Definition ---
# Define the push_subscriptions table
push_subscriptions_table = Table(
    "push_subscriptions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "subscription_json",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    ),
    Column("user_identifier", String(255), nullable=False, index=True),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)

# Note: A traditional foreign key is not used for `user_identifier` because the application
# manages users through a session-based and token-based authentication system that does not
# rely on a central `users` table. The `user_identifier` string links the subscription to
# the user's identity from the authentication system.
