from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.storage.database import Database
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.confirmation import MAX_DELEGATION_REQUEST_CHARS
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService
    from family_assistant.tools.types import (
        RequestConfirmationCallback,
        ToolDefinition,
    )


def _names(definitions: list[ToolDefinition]) -> list[str]:
    return [definition["function"]["name"] for definition in definitions]


def _delegate_policy(decision: ToolPolicyDecision) -> PolicyEngine:
    return PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            default_decision=ToolPolicyDecision.DENY,
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["delegate_to_service"]),
                    decision=decision,
                    priority=10,
                ),
            ],
        )
    )


def _exec_context(
    *,
    confirmation_callback: object | None,
    processing_service: object | None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id=None,
        db_context=MagicMock(spec=Database),
        processing_service=cast("ProcessingService", processing_service),
        request_confirmation_callback=cast(
            "RequestConfirmationCallback | None", confirmation_callback
        ),
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_confirm_gated_delegation_refuses_over_length_request() -> None:
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=_delegate_policy(ToolPolicyDecision.CONFIRM),
    )
    confirmation_callback = AsyncMock()
    context = _exec_context(
        confirmation_callback=confirmation_callback, processing_service=None
    )

    result = await policy_provider.execute_tool(
        "delegate_to_service",
        {
            "target_service_id": "engineer",
            "user_request": "x" * (MAX_DELEGATION_REQUEST_CHARS + 1),
        },
        context,
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert str(MAX_DELEGATION_REQUEST_CHARS) in result.text
    # The over-long request is refused before a confirmation prompt is ever shown.
    confirmation_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_delegation_is_not_size_capped() -> None:
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=_delegate_policy(ToolPolicyDecision.ALLOW),
    )
    # No registry, so the underlying tool reports that — proving the size guard
    # did NOT short-circuit an ordinary (unconfirmed) hand-off.
    source_service = SimpleNamespace(processing_services_registry=None)
    context = _exec_context(
        confirmation_callback=None, processing_service=source_service
    )

    result = await policy_provider.execute_tool(
        "delegate_to_service",
        {
            "target_service_id": "complex_tasks",
            "user_request": "x" * (MAX_DELEGATION_REQUEST_CHARS + 1),
        },
        context,
    )

    text = result.get_text() if isinstance(result, ToolResult) else str(result)
    assert str(MAX_DELEGATION_REQUEST_CHARS) not in text
    assert "registry is not available" in text


@pytest.mark.asyncio
async def test_real_local_catalog_is_filtered_by_policy_for_advertisement() -> None:
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=local_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                    ),
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=20,
                    ),
                ],
            )
        ),
    )

    without_confirm = await policy_provider.get_tool_definitions(can_confirm=False)
    with_confirm = await policy_provider.get_tool_definitions(can_confirm=True)

    assert "get_note" in _names(without_confirm)
    assert "delete_note" not in _names(without_confirm)
    assert "delete_note" in _names(with_confirm)
