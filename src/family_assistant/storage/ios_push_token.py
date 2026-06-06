"""iOS APNs device token storage models and table definition."""

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Integer, String, Table
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata


# --- Pydantic Model for type-safe data transfer ---
class IosPushToken(BaseModel):
    """Pydantic model for a registered iOS APNs device token."""

    id: int
    device_token: str
    user_identifier: str
    environment: str
    bundle_id: str | None
    created_at: datetime
    updated_at: datetime | None


# --- SQLAlchemy Core Table Definition ---
ios_push_tokens_table = Table(
    "ios_push_tokens",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("device_token", String(255), nullable=False, unique=True, index=True),
    Column("user_identifier", String(255), nullable=False, index=True),
    Column("environment", String(20), nullable=False, server_default="production"),
    Column("bundle_id", String(255), nullable=True),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("updated_at", DateTime(timezone=True), nullable=True),
)

# Note: As with push_subscriptions, no foreign key is used for `user_identifier`. Users are
# managed through session-based and token-based authentication rather than a central users table.
# The `device_token` is unique to a device/app install, so registration is an upsert keyed on it.
