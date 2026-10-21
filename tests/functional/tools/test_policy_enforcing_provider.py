from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.storage.database import Database
from family_assistant.tools.catalog import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing import ProcessingService
    from family_assistant.tools.types import (
        RequestConfirmationCallback,
        ToolArguments,
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


class _RejectingConfirmationCallback:
    """Records each confirmation request and rejects it."""

    def __init__(self) -> None:
        self.requested_tools: list[str] = []

    async def __call__(
        self,
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        self.requested_tools.append(tool_name)
        return ConfirmationOutcome(kind="rejected", result="no")


def _exec_context(
    *,
    db_engine: AsyncEngine,
    confirmation_callback: object | None,
    processing_service: object | None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="User",
        turn_id=None,
        db_context=Database(engine=db_engine),
        processing_service=cast("ProcessingService", processing_service),
        request_confirmation_callback=cast(
            "RequestConfirmationCallback | None", confirmation_callback
        ),
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_confirm_gated_delegation_prompts_for_a_long_request(
    db_engine: AsyncEngine,
) -> None:
    # A large payload is put to the approver rather than refused before anyone
    # is asked (docs/design/confirmation-prompt-capacity.md).
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=_delegate_policy(ToolPolicyDecision.CONFIRM),
    )
    confirmation_callback = _RejectingConfirmationCallback()
    context = _exec_context(
        db_engine=db_engine,
        confirmation_callback=confirmation_callback,
        processing_service=None,
    )

    await policy_provider.execute_tool(
        "delegate_to_service",
        {
            "target_service_id": "engineer",
            "user_request": "x" * 20_000,
        },
        context,
    )

    assert confirmation_callback.requested_tools == ["delegate_to_service"]


@pytest.mark.asyncio
async def test_unconfirmed_delegation_is_not_size_capped(
    db_engine: AsyncEngine,
) -> None:
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=_delegate_policy(ToolPolicyDecision.ALLOW),
    )
    # No registry, so the underlying tool reports that — proving nothing
    # short-circuited an ordinary (unconfirmed) hand-off.
    source_service = SimpleNamespace(processing_services_registry=None)
    context = _exec_context(
        db_engine=db_engine,
        confirmation_callback=None,
        processing_service=source_service,
    )

    result = await policy_provider.execute_tool(
        "delegate_to_service",
        {
            "target_service_id": "complex_tasks",
            "user_request": "x" * 20_000,
        },
        context,
    )

    text = result.get_text() if isinstance(result, ToolResult) else str(result)
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
