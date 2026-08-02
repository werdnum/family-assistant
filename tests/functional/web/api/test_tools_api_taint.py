"""Runtime-taint propagation through the direct tool execution API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


async def _descriptor_tainted_tool() -> ToolResult:
    return ToolResult(text="external descriptor result")


def _set_profile_tools_provider(app: FastAPI, provider: object) -> None:
    app.state.processing_service.tools_provider = provider


class TaintingDirectToolsProvider:
    """Fake direct provider that exposes and then adds runtime taint."""

    def __init__(self) -> None:
        self.initial_tiers: list[SourceTrustTier] = []
        self.context_tools_providers: list[object | None] = []
        self.context_timezones: list[ZoneInfo] = []

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - fake provider accepts arbitrary tool arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> ToolResult:
        del name, arguments, call_id
        assert context.taint_tracker is not None
        self.initial_tiers.append(context.taint_tracker.snapshot().max_tier)
        self.context_tools_providers.append(context.tools_provider)
        self.context_timezones.append(context.timezone)
        context.taint_tracker.add_source(
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="voice-email",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="voice tool returned an external email",
            )
        )
        return ToolResult(text="external result")

    async def close(self) -> None:
        return None


class FailingTaintingDirectToolsProvider(TaintingDirectToolsProvider):
    """Fake provider whose failed call still introduces runtime taint."""

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - fake provider accepts arbitrary tool arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> ToolResult:
        await super().execute_tool(name, arguments, context, call_id)
        await context.db_context.message_history.add_message(
            UserMessage(content="must roll back"),
            interface_type="api",
            conversation_id="failed-direct-tool",
            timestamp=datetime.now(UTC),
            turn_id=context.turn_id,
            user_id=context.user_id,
            processing_profile_id=context.processing_profile_id,
        )
        raise RuntimeError("tool failed after reading external data")


@pytest.mark.asyncio
async def test_direct_tool_api_threads_taint_between_voice_calls(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    provider = TaintingDirectToolsProvider()
    app_fixture.state.tools_provider = provider
    _set_profile_tools_provider(app_fixture, provider)

    first = await api_test_client.post(
        "/api/tools/execute/read_external",
        json={
            "arguments": {},
            "taint_metadata": TurnTaintState.empty().to_metadata(),
        },
    )
    assert first.status_code == 200
    first_taint = first.json()["taint_metadata"]
    assert first_taint["max_tier"] == "unknown_external"

    second = await api_test_client.post(
        "/api/tools/execute/read_external",
        json={"arguments": {}, "taint_metadata": first_taint},
    )
    assert second.status_code == 200
    assert provider.initial_tiers == [
        SourceTrustTier.TRUSTED_USER,
        SourceTrustTier.UNKNOWN_EXTERNAL,
    ]


@pytest.mark.asyncio
async def test_direct_tool_api_missing_taint_is_conservative(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    provider = TaintingDirectToolsProvider()
    app_fixture.state.tools_provider = provider
    _set_profile_tools_provider(app_fixture, provider)

    response = await api_test_client.post(
        "/api/tools/execute/read_external",
        json={"arguments": {}},
    )

    assert response.status_code == 200
    assert provider.initial_tiers == [SourceTrustTier.UNKNOWN_EXTERNAL]


@pytest.mark.asyncio
async def test_direct_tool_api_failure_returns_accumulated_taint(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    provider = FailingTaintingDirectToolsProvider()
    app_fixture.state.tools_provider = provider
    _set_profile_tools_provider(app_fixture, provider)

    response = await api_test_client.post(
        "/api/tools/execute/read_external",
        json={
            "arguments": {},
            "taint_metadata": TurnTaintState.empty().to_metadata(),
        },
    )

    assert response.status_code == 500
    assert response.json()["taint_metadata"]["max_tier"] == "unknown_external"
    # The request was received, so its user message is durable; what a failed
    # tool must not leave behind is an assistant reply.
    db_context = Database(db_engine)
    messages = await db_context.message_history.get_recent(
        interface_type="api",
        conversation_id="failed-direct-tool",
    )
    assert [message for message in messages if message.role == "assistant"] == []


@pytest.mark.asyncio
async def test_direct_tool_api_uses_selected_provider_for_nested_tools(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    root_provider = TaintingDirectToolsProvider()
    profile_provider = TaintingDirectToolsProvider()
    app_fixture.state.tools_provider = root_provider
    processing_service = app_fixture.state.processing_service
    processing_service.tools_provider = profile_provider
    processing_service.service_config.timezone = ZoneInfo("Pacific/Auckland")
    profile_id = processing_service.service_config.id
    app_fixture.state.processing_services = {profile_id: processing_service}

    response = await api_test_client.post(
        "/api/tools/execute/read_external",
        json={
            "arguments": {},
            "profile_id": profile_id,
            "taint_metadata": TurnTaintState.empty().to_metadata(),
        },
    )

    assert response.status_code == 200
    assert profile_provider.context_tools_providers == [profile_provider]
    assert profile_provider.context_timezones == [ZoneInfo("Pacific/Auckland")]
    assert root_provider.initial_tiers == []


@pytest.mark.asyncio
async def test_direct_tool_api_uses_selected_profile_taint_provider(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    profile_provider = TaintTrackingToolsProvider(
        LocalToolsProvider(
            registrations=[
                ToolRegistration(
                    definition=cast(
                        "ToolDefinition",
                        {
                            "type": "function",
                            "function": {
                                "name": "descriptor_external",
                                "description": "Return untrusted external data.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ),
                    implementation=_descriptor_tainted_tool,
                    metadata=make_local_tool_metadata([
                        ToolTag.READ_ONLY,
                        ToolTag.OUTPUT_UNTRUSTED,
                    ]),
                )
            ]
        )
    )
    processing_service = app_fixture.state.processing_service
    processing_service.tools_provider = profile_provider
    profile_id = processing_service.service_config.id
    app_fixture.state.processing_services = {profile_id: processing_service}

    response = await api_test_client.post(
        "/api/tools/execute/descriptor_external",
        json={
            "arguments": {},
            "profile_id": profile_id,
            "taint_metadata": TurnTaintState.empty().to_metadata(),
        },
    )

    assert response.status_code == 200
    assert response.json()["taint_metadata"]["max_tier"] == "unknown_external"
