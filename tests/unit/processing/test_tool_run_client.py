"""A tool that calls a model calls the one its turn is running on.

The tier a turn resolved to decides which client serves it, and a tool that
reached for the service's own ``llm_client`` instead would spend at the
profile's default tier while the turn's telemetry said otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
    ResolvedModelSelection,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    LocalToolMetadata,
    ToolRegistration,
    ToolTag,
)
from family_assistant.tools.types import ToolResult
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module - note: pylint cannot resolve the implicit `tests` namespace package
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

PROFILE = "tool-run-client"

_ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(
        ModelTierOption(id="standard", label="Standard"),
        ModelTierOption(id="deep", label="Deep"),
    ),
    auto=frozenset({"standard", "deep"}),
)


def _make_service(
    seen: list[LLMInterface | None],
    tier_clients: dict[str, LLMInterface],
) -> ProcessingService:
    async def record_run_client(exec_context: ToolExecutionContext) -> ToolResult:
        seen.append(exec_context.llm_client)
        return ToolResult(text="recorded")

    definition = cast(
        "ToolDefinition",
        {
            "type": "function",
            "function": {
                "name": "record_run_client",
                "description": "Record the client this turn is bound to.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    )
    return ProcessingService(
        llm_client=tier_clients["standard"],
        tools_provider=LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=definition,
                    implementation=record_run_client,
                    metadata=LocalToolMetadata(tags=frozenset({ToolTag.READ_ONLY})),
                )
            ]
        ),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            max_history_messages=5,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            id=PROFILE,
            tier_eligibility=_ELIGIBILITY,
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        tier_llm_clients=tier_clients,
    )


def _client_calling_the_tool_once() -> RuleBasedMockLLMClient:
    """Answers with the tool call first, then with plain text."""
    return RuleBasedMockLLMClient(
        rules=[
            (
                lambda kwargs: (
                    not any(
                        getattr(message, "role", None) == "tool"
                        for message in kwargs["messages"]
                    )
                ),
                LLMOutput(
                    tool_calls=[
                        ToolCallItem(
                            id="call-1",
                            type="function",
                            function=ToolCallFunction(
                                name="record_run_client", arguments="{}"
                            ),
                        )
                    ]
                ),
            )
        ],
        default_response=LLMOutput(content="done"),
    )


@pytest.mark.asyncio
async def test_a_tool_sees_the_client_its_turn_resolved_to(
    db_engine: AsyncEngine,
) -> None:
    seen: list[LLMInterface | None] = []
    tier_clients: dict[str, LLMInterface] = {
        "standard": _client_calling_the_tool_once(),
        "deep": _client_calling_the_tool_once(),
    }
    service = _make_service(seen, tier_clients)

    await service.handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="web",
        conversation_id="conversation-tool-run-client",
        trigger_content_parts=[{"type": "text", "text": "Use the tool"}],
        trigger_interface_message_id=None,
        user_name="Test User",
        model_selection=ResolvedModelSelection(
            tier="deep", requested="deep", source="user"
        ),
    )

    assert seen == [tier_clients["deep"]]
