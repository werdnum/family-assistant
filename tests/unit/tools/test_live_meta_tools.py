"""Unit tests for the Live (voice) meta-tools.

A Gemini Live session cannot be handed a new tool declaration once it has
started, so on-demand tools are reached through ``search_tools`` and
``call_tool`` instead. These tests pin what a live session may declare, what a
search hands back, and that ``call_tool`` reaches the real provider chain
rather than a path of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.live_meta import (
    CALL_TOOL_TOOL_NAME,
    SEARCH_TOOLS_TOOL_NAME,
    LiveMetaToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandToolsView
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

CALLS: list[tuple[str, dict[str, object]]] = []


async def _record_call(**kwargs: object) -> str:
    CALLS.append(("called", dict(kwargs)))
    return "done"


def _registration(
    name: str,
    *,
    description: str,
    # ast-grep-ignore: no-dict-any - JSON Schema fragment for a test fixture
    properties: dict[str, Any] | None = None,
) -> ToolRegistration:
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties or {},
                    },
                },
            },
        ),
        implementation=_record_call,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )


def _build_provider(
    *, on_demand_names: set[str] | None = None
) -> LiveMetaToolsProvider:
    local_provider = LocalToolsProvider(
        registrations=[
            _registration("list_notes", description="List the household's notes."),
            _registration(
                "call_home_assistant_action",
                description="Run a Home Assistant action on an entity.",
                properties={
                    "entity_id": {
                        "type": "string",
                        "description": "Entity to act on.",
                    },
                    "action": {"type": "string", "description": "Action name."},
                },
            ),
            _registration(
                "generate_image", description="Draw a picture from a prompt."
            ),
        ]
    )
    view = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names=(
            {"call_home_assistant_action", "generate_image"}
            if on_demand_names is None
            else on_demand_names
        ),
    )
    return LiveMetaToolsProvider(view)


def _context() -> ToolExecutionContext:
    return cast("ToolExecutionContext", object())


@pytest.fixture(autouse=True)
def _clear_calls() -> None:
    CALLS.clear()


@pytest.mark.asyncio
async def test_declarations_replace_on_demand_tools_with_meta_tools() -> None:
    provider = _build_provider()

    names = {
        definition["function"]["name"]
        for definition in await provider.get_tool_definitions()
    }

    assert names == {"list_notes", SEARCH_TOOLS_TOOL_NAME, CALL_TOOL_TOOL_NAME}


@pytest.mark.asyncio
async def test_no_meta_tools_when_nothing_is_on_demand() -> None:
    provider = _build_provider(on_demand_names=set())

    names = {
        definition["function"]["name"]
        for definition in await provider.get_tool_definitions()
    }

    assert names == {"list_notes", "call_home_assistant_action", "generate_image"}


@pytest.mark.asyncio
async def test_catalog_lists_the_reachable_tools() -> None:
    provider = _build_provider()

    addition = await provider.get_system_prompt_addition()

    assert addition is not None
    assert "call_home_assistant_action" in addition
    assert "generate_image" in addition
    assert "search_tools" in addition
    assert "list_notes" not in addition


@pytest.mark.asyncio
async def test_search_returns_the_full_argument_schema() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        SEARCH_TOOLS_TOOL_NAME, {"query": "home assistant action"}, _context()
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.get_data())
    top = data["tools"][0]
    assert top["name"] == "call_home_assistant_action"
    assert top["description"] == "Run a Home Assistant action on an entity."
    assert set(top["parameters"]["properties"]) == {"entity_id", "action"}
    assert top["already_declared"] is False


@pytest.mark.asyncio
async def test_search_marks_declared_tools_so_the_model_calls_them_directly() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        SEARCH_TOOLS_TOOL_NAME, {"query": "notes"}, _context()
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.get_data())
    assert data["tools"][0]["name"] == "list_notes"
    assert data["tools"][0]["already_declared"] is True


@pytest.mark.asyncio
async def test_search_without_a_query_lists_everything_within_the_limit() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        SEARCH_TOOLS_TOOL_NAME, {"limit": 2}, _context()
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.get_data())
    assert data["match_count"] == 3
    assert data["returned"] == 2


@pytest.mark.asyncio
async def test_search_miss_tells_the_model_how_to_recover() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        SEARCH_TOOLS_TOOL_NAME, {"query": "zzzznotathing"}, _context()
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.get_data())
    assert data["tools"] == []
    assert "hint" in data


@pytest.mark.asyncio
async def test_call_tool_runs_the_named_tool_with_decoded_arguments() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {
            "name": "call_home_assistant_action",
            "arguments_json": '{"entity_id": "light.kitchen", "action": "turn_on"}',
        },
        _context(),
    )

    assert result == "done"
    assert CALLS == [
        ("called", {"entity_id": "light.kitchen", "action": "turn_on"}),
    ]


@pytest.mark.asyncio
async def test_call_tool_accepts_an_object_despite_the_string_schema() -> None:
    provider = _build_provider()

    await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {
            "name": "call_home_assistant_action",
            "arguments_json": {"entity_id": "light.kitchen", "action": "turn_off"},
        },
        _context(),
    )

    assert CALLS == [
        ("called", {"entity_id": "light.kitchen", "action": "turn_off"}),
    ]


@pytest.mark.asyncio
async def test_call_tool_omitting_arguments_runs_with_none() -> None:
    provider = _build_provider()

    await provider.execute_tool(
        CALL_TOOL_TOOL_NAME, {"name": "generate_image"}, _context()
    )

    assert CALLS == [("called", {})]


@pytest.mark.asyncio
async def test_call_tool_reports_malformed_json_without_running_anything() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {"name": "generate_image", "arguments_json": "{not json"},
        _context(),
    )

    assert isinstance(result, ToolResult)
    assert "not valid JSON" in (result.text or "")
    assert CALLS == []


@pytest.mark.asyncio
async def test_call_tool_rejects_a_tool_the_session_may_not_run() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME, {"name": "delete_everything"}, _context()
    )

    assert isinstance(result, ToolResult)
    assert "not available" in (result.text or "")
    assert CALLS == []


@pytest.mark.asyncio
async def test_call_tool_refuses_to_call_itself() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {"name": CALL_TOOL_TOOL_NAME, "arguments_json": "{}"},
        _context(),
    )

    assert isinstance(result, ToolResult)
    assert "cannot run" in (result.text or "")


@pytest.mark.asyncio
async def test_call_tool_without_a_name_asks_for_one() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(CALL_TOOL_TOOL_NAME, {}, _context())

    assert isinstance(result, ToolResult)
    assert "needs a 'name'" in (result.text or "")


@pytest.mark.asyncio
async def test_non_meta_names_are_delegated_unchanged() -> None:
    provider = _build_provider()

    result = await provider.execute_tool("list_notes", {}, _context())

    assert result == "done"
    assert CALLS == [("called", {})]
