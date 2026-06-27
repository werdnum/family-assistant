from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.confirmation import MAX_DELEGATION_REQUEST_CHARS
from family_assistant.tools.services import delegate_to_service_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService


@pytest.mark.asyncio
async def test_delegate_to_service_blocks_disallowed_source_profile() -> None:
    source_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="source_profile",
            tools_config=SimpleNamespace(confirmation_timeout_seconds=10.0),
        ),
    )
    target_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="target_profile",
            allowed_delegation_sources=["other_profile"],
        ),
    )
    source_service.processing_services_registry = {"target_profile": target_service}

    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id=None,
        db_context=MagicMock(spec=DatabaseContext),
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
    )

    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request="do work",
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert "source_profile" in result.text
    assert "not permitted to delegate" in result.text


@pytest.mark.asyncio
async def test_delegate_to_service_refuses_over_length_request_when_confirming() -> (
    None
):
    target_handler = AsyncMock()
    target_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="target_profile",
            allowed_delegation_sources=None,
        ),
        handle_chat_interaction=target_handler,
    )
    source_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="source_profile",
            tools_config=SimpleNamespace(confirmation_timeout_seconds=10.0),
        ),
        processing_services_registry={"target_profile": target_service},
    )

    context = ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id=None,
        db_context=MagicMock(spec=DatabaseContext),
        processing_service=cast("ProcessingService", source_service),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
    )

    over_limit = "x" * (MAX_DELEGATION_REQUEST_CHARS + 1)
    # confirm_delegation=True means this hand-off will be approved against a
    # confirmation prompt, so the over-long request must be refused.
    result = await delegate_to_service_tool(
        exec_context=context,
        target_service_id="target_profile",
        user_request=over_limit,
        confirm_delegation=True,
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert str(MAX_DELEGATION_REQUEST_CHARS) in result.text
    assert "exceeds" in result.text
    # The over-long request must never reach the target profile.
    target_handler.assert_not_awaited()
