"""Pydantic models for storage layer data structures."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Automation(BaseModel):
    """Unified Pydantic model for both event and schedule automations.

    This model represents the complete automation data structure, combining fields
    from both event listeners and schedule automations. Type-specific fields are
    optional and null for the other type.
    """

    id: int
    type: Literal["event", "schedule"] = Field(..., description="Automation type")
    name: str
    description: str | None = None
    conversation_id: str
    interface_type: str
    action_type: str
    action_config: dict[str, Any]
    enabled: bool
    created_at: datetime
    last_execution_at: datetime | None = None

    # Event-specific fields (null for schedule automations)
    source_id: str | None = None
    match_conditions: dict[str, Any] | None = None
    condition_script: str | None = None
    one_time: bool | None = None
    daily_executions: int | None = None
    daily_reset_at: datetime | None = None

    # Schedule-specific fields (null for event automations)
    recurrence_rule: str | None = None
    next_scheduled_at: datetime | None = None
    execution_count: int | None = None

    class Config:
        """Pydantic model configuration."""

        frozen = True
