"""Runtime-taint propagation through the direct tool execution API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient

    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class TaintingDirectToolsProvider:
    """Fake direct provider that exposes and then adds runtime taint."""

    def __init__(self) -> None:
        self.initial_tiers: list[SourceTrustTier] = []

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
        raise RuntimeError("tool failed after reading external data")


@pytest.mark.asyncio
async def test_direct_tool_api_threads_taint_between_voice_calls(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    provider = TaintingDirectToolsProvider()
    app_fixture.state.tools_provider = provider

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
) -> None:
    app_fixture.state.tools_provider = FailingTaintingDirectToolsProvider()

    response = await api_test_client.post(
        "/api/tools/execute/read_external",
        json={
            "arguments": {},
            "taint_metadata": TurnTaintState.empty().to_metadata(),
        },
    )

    assert response.status_code == 500
    assert response.json()["taint_metadata"]["max_tier"] == "unknown_external"
