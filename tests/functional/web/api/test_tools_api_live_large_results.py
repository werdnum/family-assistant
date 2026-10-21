"""A large tool result reaching a Live (voice) session as an attachment.

A Live model prefills the whole function response before it speaks, so the
voice clients' tool path stores a result past the live threshold as an
attachment and hands the model a pointer it can follow with ``jq_query``.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from family_assistant.tools.catalog import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolImplementation,
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

ENTITY_COUNT = 2000
ATTACHMENT_ID_PATTERN = re.compile(r"saved as attachment ([0-9a-f-]{36})")


async def _home_overview() -> ToolResult:
    return ToolResult(
        data={
            "price": {"low": 0.07, "high": 0.13},
            "entities": [f"sensor.reading_{i}" for i in range(ENTITY_COUNT)],
        }
    )


async def _home_price() -> ToolResult:
    return ToolResult(data={"price": {"low": 0.07, "high": 0.13}})


def _registration(name: str, implementation: ToolImplementation) -> ToolRegistration:
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Read the house.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        implementation=implementation,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


def _install_provider(app: FastAPI, *, with_readers: bool = True) -> None:
    readers = {"jq_query", "read_text_attachment"} if with_readers else set()
    provider = LocalToolsProvider(
        registrations=[
            _registration("home_overview", _home_overview),
            _registration("home_price", _home_price),
            *(r for r in LOCAL_TOOL_REGISTRATIONS if r.name in readers),
        ]
    )
    service = app.state.processing_service
    service.tools_provider = provider
    service.on_demand_view = OnDemandToolsView(
        wrapped_provider=provider,
        on_demand_tool_names={"home_overview", "home_price"},
    )


def _voice_call(name: str, arguments_json: str = "{}") -> dict[str, object]:
    return {
        "voice_conversation_id": f"web_conv_{uuid4()}",
        "arguments": {"name": name, "arguments_json": arguments_json},
    }


@pytest.mark.asyncio
async def test_large_voice_result_arrives_as_an_attachment_the_model_can_query(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    _install_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool", json=_voice_call("home_overview")
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert "data" not in result
    assert "jq_query" in result["text"]
    match = ATTACHMENT_ID_PATTERN.search(result["text"])
    assert match is not None, result["text"]

    follow_up = await api_test_client.post(
        "/api/tools/execute/jq_query",
        json={
            "voice_conversation_id": f"web_conv_{uuid4()}",
            "arguments": {"attachment_id": match.group(1), "jq_program": ".price"},
        },
    )

    assert follow_up.status_code == 200, follow_up.text
    follow_up_result = follow_up.json()["result"]
    queried = follow_up_result.get("data") or json.loads(follow_up_result["text"])
    assert queried == {"low": 0.07, "high": 0.13}


@pytest.mark.asyncio
async def test_reading_the_attachment_through_call_tool_returns_its_content(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """The reader's exemption holds when the session reaches it via call_tool."""
    _install_provider(app_fixture)
    converted = await api_test_client.post(
        "/api/tools/execute/call_tool", json=_voice_call("home_overview")
    )
    match = ATTACHMENT_ID_PATTERN.search(converted.json()["result"]["text"])
    assert match is not None, converted.text

    response = await api_test_client.post(
        "/api/tools/execute/call_tool",
        json=_voice_call(
            "read_text_attachment",
            json.dumps({"attachment_id": match.group(1), "limit": 1000}),
        ),
    )

    assert response.status_code == 200, response.text
    assert "sensor.reading_900" in response.text
    assert ATTACHMENT_ID_PATTERN.search(response.text) is None


@pytest.mark.asyncio
async def test_profile_without_a_reader_gets_the_whole_large_result(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """A pointer the session has no tool to follow would leave it with nothing."""
    _install_provider(app_fixture, with_readers=False)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool", json=_voice_call("home_overview")
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["result"]["data"]["entities"]) == ENTITY_COUNT


@pytest.mark.asyncio
async def test_small_voice_result_is_returned_whole(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    _install_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool", json=_voice_call("home_price")
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["data"] == {"price": {"low": 0.07, "high": 0.13}}


@pytest.mark.asyncio
async def test_non_voice_call_keeps_the_whole_result(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """The tools page calls the endpoint without a voice conversation."""
    _install_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool",
        json={"arguments": {"name": "home_overview", "arguments_json": "{}"}},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["result"]["data"]["entities"]) == ENTITY_COUNT
