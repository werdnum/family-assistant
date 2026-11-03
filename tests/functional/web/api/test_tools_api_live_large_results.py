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
from family_assistant.tools.live_meta import SKILL_TOOLS_KEY
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


POOL_ENTITIES = ["sensor.pool_temperature", "sensor.spa_temperature"]


async def _home_entities() -> ToolResult:
    return ToolResult(
        text=f"Found {len(POOL_ENTITIES)} entities: {', '.join(POOL_ENTITIES)}",
        data={"entities": POOL_ENTITIES},
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


def _install_provider(
    app: FastAPI,
    *,
    with_readers: bool = True,
    extra: list[ToolRegistration] | None = None,
) -> None:
    readers = {"jq_query", "read_text_attachment"} if with_readers else set()
    provider = LocalToolsProvider(
        registrations=[
            _registration("home_overview", _home_overview),
            _registration("home_price", _home_price),
            _registration("home_entities", _home_entities),
            *(r for r in LOCAL_TOOL_REGISTRATIONS if r.name in readers),
            *(extra or []),
        ]
    )
    service = app.state.processing_service
    service.tools_provider = provider
    service.on_demand_view = OnDemandToolsView(
        wrapped_provider=provider,
        on_demand_tool_names={"home_overview", "home_price", "home_entities"},
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
            json.dumps({"attachment_id": match.group(1), "grep": "reading_900"}),
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


@pytest.mark.asyncio
async def test_voice_result_with_text_carries_the_text_alone(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """``data`` restates the text for scripts; the model needs one of them."""
    _install_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool", json=_voice_call("home_entities")
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "text": "Found 2 entities: sensor.pool_temperature, sensor.spa_temperature"
    }


@pytest.mark.asyncio
async def test_non_voice_call_keeps_both_text_and_data(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    _install_provider(app_fixture)

    response = await api_test_client.post(
        "/api/tools/execute/call_tool",
        json={"arguments": {"name": "home_entities", "arguments_json": "{}"}},
    )

    assert response.status_code == 200, response.text
    assert set(response.json()["result"]) == {"text", "data"}


@pytest.mark.asyncio
async def test_large_search_result_is_never_converted(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """A pointer to the schema would only cost a round trip to read it back."""
    _install_provider(app_fixture)
    app_fixture.state.processing_service.app_config.gemini_live_config.large_tool_result_threshold_kb = 1

    response = await api_test_client.post(
        "/api/tools/execute/search_tools",
        json={
            "voice_conversation_id": f"web_conv_{uuid4()}",
            "arguments": {"limit": 15},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["data"]["tools"]


@pytest.mark.asyncio
async def test_large_skill_with_tools_is_never_converted(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """A pointer would leave the model without the tools the skill brought."""

    async def get_note() -> ToolResult:
        return ToolResult(
            data={"content": "x" * 4096, "activate_tools": ["home_price"]}
        )

    _install_provider(app_fixture, extra=[_registration("get_note", get_note)])
    app_fixture.state.processing_service.app_config.gemini_live_config.large_tool_result_threshold_kb = 1

    response = await api_test_client.post(
        "/api/tools/execute/get_note",
        json={"voice_conversation_id": f"web_conv_{uuid4()}", "arguments": {}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["data"][SKILL_TOOLS_KEY][0]["name"] == "home_price"


@pytest.mark.asyncio
async def test_voice_excluded_tool_is_refused_when_called_directly(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """A Live client runs whatever name the model emits, declared or not."""
    _install_provider(app_fixture)
    app_fixture.state.processing_service.app_config.gemini_live_config.tools.excluded_tools = [
        "home_price"
    ]

    response = await api_test_client.post(
        "/api/tools/execute/home_price",
        json={"voice_conversation_id": f"web_conv_{uuid4()}", "arguments": {}},
    )

    assert response.status_code == 200, response.text
    assert "not available in voice sessions" in response.json()["result"]["text"]


@pytest.mark.asyncio
async def test_voice_excluded_tool_still_runs_outside_voice(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
) -> None:
    """The tools page reaches the same endpoint without a voice conversation."""
    _install_provider(app_fixture)
    app_fixture.state.processing_service.app_config.gemini_live_config.tools.excluded_tools = [
        "home_price"
    ]

    response = await api_test_client.post(
        "/api/tools/execute/home_price", json={"arguments": {}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["data"] == {"price": {"low": 0.07, "high": 0.13}}
