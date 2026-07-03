"""Functional tests for ops_automation confinement (Phase 2).

Covers the execute_action wake_llm runtime guard and the cross-profile
update_automation denial against a real database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.actions import (
    ActionType,
    WakeLlmProfileError,
    execute_action,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.automations import (
    create_automation_tool,
    update_automation_tool,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _exec_context(
    db_ctx: DatabaseContext,
    *,
    conversation_id: str,
    processing_profile_id: str | None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=conversation_id,
        user_name="tester",
        turn_id="turn",
        db_context=db_ctx,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        processing_profile_id=processing_profile_id,
        user_id="user-1",
    )


# --- execute_action wake_llm runtime guard ---


@pytest.mark.asyncio
async def test_execute_action_refuses_confined_wake_llm(
    db_engine: AsyncEngine,
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        with pytest.raises(WakeLlmProfileError):
            await execute_action(
                db_ctx=db,
                action_type=ActionType.WAKE_LLM,
                action_config={"context": "diagnostics summary"},
                conversation_id="conv",
                processing_profile_id="ops_automation",
                default_profile_id="default_assistant",
            )


@pytest.mark.asyncio
async def test_execute_action_allows_default_wake_llm(
    db_engine: AsyncEngine,
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        # Stamped with the default profile -> enqueues without raising.
        await execute_action(
            db_ctx=db,
            action_type=ActionType.WAKE_LLM,
            action_config={"context": "hello"},
            conversation_id="conv",
            processing_profile_id="default_assistant",
            default_profile_id="default_assistant",
        )


# --- cross-profile update_automation denial ---


@pytest.mark.asyncio
async def test_cross_profile_update_denied(db_engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        owner_ctx = _exec_context(
            db,
            conversation_id="conv_owner",
            processing_profile_id="ops_automation",
        )
        created = await create_automation_tool(
            exec_context=owner_ctx,
            name="Owned Schedule",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
            action_type="script",
            action_config={"script_code": "x = 1\n"},
        )
        created_data = created.get_data()
        assert isinstance(created_data, dict)
        automation_id = int(created_data["id"])

        # A different profile cannot update the automation.
        other_ctx = _exec_context(
            db,
            conversation_id="conv_owner",
            processing_profile_id="default_assistant",
        )
        result = await update_automation_tool(
            exec_context=other_ctx,
            automation_id=automation_id,
            automation_type="schedule",
            description="hijacked",
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "owned by profile" in data["error"].lower()


@pytest.mark.asyncio
async def test_same_profile_update_allowed(db_engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        owner_ctx = _exec_context(
            db,
            conversation_id="conv_same",
            processing_profile_id="ops_automation",
        )
        created = await create_automation_tool(
            exec_context=owner_ctx,
            name="Owned Schedule Same",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
            action_type="script",
            action_config={"script_code": "x = 1\n"},
        )
        created_data = created.get_data()
        assert isinstance(created_data, dict)
        automation_id = int(created_data["id"])

        result = await update_automation_tool(
            exec_context=owner_ctx,
            automation_id=automation_id,
            automation_type="schedule",
            description="updated by owner",
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" not in data
