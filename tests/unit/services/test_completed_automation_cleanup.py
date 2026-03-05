"""Tests for the completed automation cleanup handler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import update

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import event_listeners_table
from family_assistant.task_worker import handle_completed_automation_cleanup

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class MinimalContext:
    """Minimal context for testing cleanup handler."""

    interface_type: str
    conversation_id: str
    user_name: str
    db_context: DatabaseContext
    processing_service: None = None


@pytest.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    """Create a database context for testing."""
    async with DatabaseContext(engine=db_engine) as context:
        yield context


@pytest.fixture
def exec_context(db_context: DatabaseContext) -> MinimalContext:
    """Create a minimal execution context for testing."""
    return MinimalContext(
        interface_type="test",
        conversation_id="test-conv-123",
        user_name="test_user",
        db_context=db_context,
    )


class TestCompletedAutomationCleanup:
    """Tests for handle_completed_automation_cleanup."""

    @pytest.mark.asyncio
    async def test_deletes_old_completed_one_time_listeners(
        self, exec_context: MinimalContext, db_context: DatabaseContext
    ) -> None:
        """Test that completed one-time listeners older than retention are deleted."""
        # Create a one-time listener
        listener_id = await db_context.events.create_event_listener(
            name="old-one-time",
            source_id="home_assistant",
            match_conditions={"type": "test"},
            conversation_id="test-conv-123",
            one_time=True,
            enabled=True,
        )

        # Simulate execution and disabling (as the event processor would do)
        old_time = datetime.now(UTC) - timedelta(hours=48)
        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(enabled=False, last_execution_at=old_time)
        )
        await db_context.execute_with_retry(stmt)

        # Run cleanup with 24 hour retention
        await handle_completed_automation_cleanup(exec_context, {"retention_hours": 24})  # type: ignore[arg-type]

        # Listener should be deleted
        listener = await db_context.events.get_event_listener_by_id(listener_id)
        assert listener is None

    @pytest.mark.asyncio
    async def test_preserves_recently_completed_one_time_listeners(
        self, exec_context: MinimalContext, db_context: DatabaseContext
    ) -> None:
        """Test that recently completed one-time listeners are preserved."""
        listener_id = await db_context.events.create_event_listener(
            name="recent-one-time",
            source_id="home_assistant",
            match_conditions={"type": "test"},
            conversation_id="test-conv-123",
            one_time=True,
            enabled=True,
        )

        # Simulate recent execution and disabling
        recent_time = datetime.now(UTC) - timedelta(hours=1)
        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(enabled=False, last_execution_at=recent_time)
        )
        await db_context.execute_with_retry(stmt)

        await handle_completed_automation_cleanup(exec_context, {"retention_hours": 24})  # type: ignore[arg-type]

        # Listener should still exist
        listener = await db_context.events.get_event_listener_by_id(listener_id)
        assert listener is not None

    @pytest.mark.asyncio
    async def test_preserves_enabled_one_time_listeners(
        self, exec_context: MinimalContext, db_context: DatabaseContext
    ) -> None:
        """Test that enabled (not yet fired) one-time listeners are preserved."""
        listener_id = await db_context.events.create_event_listener(
            name="unfired-one-time",
            source_id="home_assistant",
            match_conditions={"type": "test"},
            conversation_id="test-conv-123",
            one_time=True,
            enabled=True,
        )

        await handle_completed_automation_cleanup(exec_context, {"retention_hours": 24})  # type: ignore[arg-type]

        # Listener should still exist (it hasn't fired yet)
        listener = await db_context.events.get_event_listener_by_id(listener_id)
        assert listener is not None

    @pytest.mark.asyncio
    async def test_preserves_recurring_listeners(
        self, exec_context: MinimalContext, db_context: DatabaseContext
    ) -> None:
        """Test that recurring (non-one-time) disabled listeners are preserved."""
        listener_id = await db_context.events.create_event_listener(
            name="recurring-disabled",
            source_id="home_assistant",
            match_conditions={"type": "test"},
            conversation_id="test-conv-123",
            one_time=False,
            enabled=False,
        )

        await handle_completed_automation_cleanup(exec_context, {"retention_hours": 24})  # type: ignore[arg-type]

        # Recurring listener should still exist even if disabled
        listener = await db_context.events.get_event_listener_by_id(listener_id)
        assert listener is not None

    @pytest.mark.asyncio
    async def test_uses_default_retention(self, exec_context: MinimalContext) -> None:
        """Test that cleanup uses default 24h retention when not specified."""
        await handle_completed_automation_cleanup(exec_context, {})  # type: ignore[arg-type]
