"""A voice session reaching an on-demand tool through ``call_tool``.

The browser and iOS voice clients run every tool call against
``POST /api/tools/execute/{name}``, so this is the path a Live session takes to
a tool it was never handed a declaration for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandToolsView
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient

    from family_assistant.tools.types import ToolDefinition

RECEIVED: list[dict[str, object]] = []


async def _hidden_tool(**kwargs: object) -> ToolResult:
    RECEIVED.append(dict(kwargs))
    return ToolResult(text=f"lit {kwargs.get('entity_id')}")


def _registration(name: str) -> ToolRegistration:
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Turn a light on.",
                    "parameters": {
                        "type": "object",
                        "properties": {"entity_id": {"type": "string"}},
                    },
                },
            },
        ),
        implementation=_hidden_tool,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


def _install_on_demand_provider(app: FastAPI) -> None:
    provider = LocalToolsProvider(registrations=[_registration("turn_on_light")])
    service = app.state.processing_service
    service.tools_provider = provider
    service.on_demand_view = OnDemandToolsView(
        wrapped_provider=provider,
        on_demand_tool_names={"turn_on_light"},
    )


@pytest.mark.asyncio
async def test_call_tool_runs_a_tool_the_session_never_declared(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    RECEIVED.clear()
    _install_on_demand_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool",
        json={
            "arguments": {
                "name": "turn_on_light",
                "arguments_json": '{"entity_id": "light.kitchen"}',
            }
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["text"] == "lit light.kitchen"
    assert RECEIVED == [{"entity_id": "light.kitchen"}]


@pytest.mark.asyncio
async def test_search_tools_returns_the_hidden_tools_schema(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    _install_on_demand_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/search_tools",
        json={"arguments": {"query": "light"}},
    )

    assert response.status_code == 200, response.text
    data = cast("dict[str, Any]", response.json()["result"]["data"])
    assert data["tools"][0]["name"] == "turn_on_light"
    assert data["tools"][0]["parameters"]["properties"] == {
        "entity_id": {"type": "string"}
    }
