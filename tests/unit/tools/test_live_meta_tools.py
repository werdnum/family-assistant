"""Unit tests for the Live (voice) meta-tools.

A Gemini Live session cannot be handed a new tool declaration once it has
started, so on-demand tools are reached through ``search_tools`` and
``call_tool`` instead. These tests pin what a live session may declare, what a
search hands back, and that ``call_tool`` reaches the real provider chain
rather than a path of its own.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import jsonschema
import pytest

from family_assistant.tools.argument_schema import check_parameter_schema
from family_assistant.tools.catalog import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.live_meta import (
    CALL_TOOL_TOOL_NAME,
    SEARCH_TOOLS_TOOL_NAME,
    SKILL_SCHEMA_BUDGET_CHARS,
    SKILL_TOOLS_KEY,
    LiveMetaToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolDescriptor,
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandToolsView
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
    required: list[str] | None = None,
    implementation: Callable[..., Awaitable[object]] | None = None,
) -> ToolRegistration:
    # ast-grep-ignore: no-dict-any - JSON Schema fragment for a test fixture
    parameters: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = required
    return ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
        ),
        implementation=implementation or _record_call,
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
                required=["entity_id", "action"],
            ),
            _registration(
                "generate_image", description="Draw a picture from a prompt."
            ),
        ]
    )
    view = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names=(
            {
                "call_home_assistant_action",
                "generate_image",
            }
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
async def test_meta_tools_survive_an_mcp_server_that_is_down_at_setup() -> None:
    """Configured on-demand capability decides, not present availability.

    A live session declares once. If the only on-demand tools belong to an MCP
    server that is down at setup, omitting the meta-tools would cost the whole
    session its route to that server even after the health loop reconnects it.
    """
    empty_provider = LocalToolsProvider(registrations=[])
    view = OnDemandToolsView(
        wrapped_provider=empty_provider,
        on_demand_tool_names=set(),
        on_demand_mcp_server_ids={"homeassistant"},
    )
    provider = LiveMetaToolsProvider(view)

    names = {
        definition["function"]["name"]
        for definition in await provider.get_tool_definitions()
    }

    assert names == {SEARCH_TOOLS_TOOL_NAME, CALL_TOOL_TOOL_NAME}


@pytest.mark.asyncio
async def test_no_meta_tools_when_nothing_is_on_demand() -> None:
    provider = _build_provider(on_demand_names=set())

    names = {
        definition["function"]["name"]
        for definition in await provider.get_tool_definitions()
    }

    assert names == {
        "list_notes",
        "call_home_assistant_action",
        "generate_image",
    }


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
async def test_catalog_carries_names_without_summaries() -> None:
    """A summary here would be paid for twice.

    Every session's system instruction carries this catalog for its whole life,
    and ``search_tools`` returns the full description anyway when the model asks
    for one. The names are what the catalog is for: they let the model search a
    name it has already seen instead of guessing a keyword.
    """
    provider = _build_provider()

    addition = await provider.get_system_prompt_addition()

    assert addition is not None
    assert "Run a Home Assistant action on an entity." not in addition
    assert "Draw a picture from a prompt." not in addition


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
async def test_search_by_exact_name_returns_only_that_tool() -> None:
    """Every token of a name can match its siblings; the name itself cannot."""
    provider = _build_provider()

    result = await provider.execute_tool(
        SEARCH_TOOLS_TOOL_NAME, {"query": "call_home_assistant_action"}, _context()
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.get_data())
    assert [tool["name"] for tool in data["tools"]] == ["call_home_assistant_action"]
    assert set(data["tools"][0]["parameters"]["properties"]) == {"entity_id", "action"}


@pytest.mark.asyncio
async def test_search_gives_only_the_first_hit_its_schema() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(SEARCH_TOOLS_TOOL_NAME, {}, _context())

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.get_data())
    assert "parameters" in data["tools"][0]
    assert all(
        set(tool) == {"name", "summary", "already_declared"}
        for tool in data["tools"][1:]
    )


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
async def test_call_tool_rejects_invented_arguments_with_the_schema() -> None:
    """The shape a model produces when it skips search_tools and guesses."""
    provider = _build_provider()

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {
            "name": "call_home_assistant_action",
            "arguments_json": '{"name": "vacuum_clean_under_dining_table"}',
        },
        _context(),
    )

    assert isinstance(result, ToolResult)
    assert isinstance(result.data, dict)
    error = result.data["error"]
    assert error["type"] == "invalid_tool_arguments"
    assert error["tool"] == "call_home_assistant_action"
    assert error["problems"] == [
        "'entity_id' is a required property",
        "'action' is a required property",
        "'name' is not an argument of this tool",
    ]
    assert error["description"] == "Run a Home Assistant action on an entity."
    assert error["parameters"]["required"] == ["entity_id", "action"]
    assert "search_tools" not in error["message"]
    assert CALLS == []


@pytest.mark.asyncio
async def test_call_tool_rejects_a_mistyped_argument() -> None:
    provider = _build_provider()

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {
            "name": "call_home_assistant_action",
            "arguments_json": '{"entity_id": 5, "action": "turn_on"}',
        },
        _context(),
    )

    assert isinstance(result, ToolResult)
    assert isinstance(result.data, dict)
    assert result.data["error"]["problems"] == [
        "'entity_id': 5 is not of type 'string'"
    ]
    assert CALLS == []


def test_a_tool_with_a_broken_schema_fails_at_provider_construction() -> None:
    """The check call_tool relies on runs at startup, not on the first call."""
    with pytest.raises(jsonschema.SchemaError):
        LocalToolsProvider(
            registrations=[
                _registration(
                    "broken_schema",
                    description="Declares a schema jsonschema cannot check.",
                    properties={"x": {"type": "nonsense"}},
                )
            ]
        )


def test_every_registered_local_tool_declares_a_checkable_schema() -> None:
    for registration in LOCAL_TOOL_REGISTRATIONS:
        check_parameter_schema(
            registration.definition["function"].get("parameters", {})
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", ["search_tools", "call_tool", "activate_tools"])
async def test_wrapping_a_provider_that_shadows_a_meta_tool_raises(
    reserved: str,
) -> None:
    local_provider = LocalToolsProvider(
        registrations=[_registration(reserved, description="A real tool.")]
    )
    view = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names={reserved},
    )

    with pytest.raises(ValueError, match="reserved"):
        await LiveMetaToolsProvider(view).get_tool_definitions()


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", ["search_tools", "call_tool"])
async def test_shadowed_meta_tool_raises_on_direct_dispatch(reserved: str) -> None:
    """Direct execution must not slip past the reserved-name check.

    ``/api/tools/execute/{name}`` reaches ``execute_tool`` without asking for
    declarations or a catalog first, so the invariant cannot rely on a listing
    having run: interception would otherwise silently shadow the real tool.
    """
    local_provider = LocalToolsProvider(
        registrations=[_registration(reserved, description="A real tool.")]
    )
    view = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names={reserved},
    )
    provider = LiveMetaToolsProvider(view)

    with pytest.raises(ValueError, match="reserved"):
        await provider.execute_tool(reserved, {}, _context())

    assert CALLS == []


@pytest.mark.asyncio
async def test_voice_excluded_tools_hidden_from_definitions_and_catalog() -> None:
    excluded_name = "noisy_tool_search"
    local_provider = LocalToolsProvider(
        registrations=[
            _registration(excluded_name, description="Search another tool catalog."),
            _registration("get_home_status", description="Get home status."),
        ]
    )
    # Test as eager tool
    view_eager = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names=set(),
    )
    provider_eager = LiveMetaToolsProvider(
        view_eager, excluded_tool_names=frozenset({excluded_name})
    )
    defs = await provider_eager.get_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert excluded_name not in names
    assert "get_home_status" in names

    # Test as on-demand tool
    view_ondemand = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names={excluded_name},
    )
    provider_ondemand = LiveMetaToolsProvider(
        view_ondemand, excluded_tool_names=frozenset({excluded_name})
    )
    prompt = await provider_ondemand.get_system_prompt_addition()
    assert prompt is None or excluded_name not in prompt


@pytest.mark.asyncio
async def test_voice_excluded_tools_rejected_on_call() -> None:
    excluded_name = "noisy_tool_search"
    local_provider = LocalToolsProvider(
        registrations=[
            _registration(excluded_name, description="Search another tool catalog."),
        ]
    )
    view = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names={excluded_name},
    )
    provider = LiveMetaToolsProvider(
        view, excluded_tool_names=frozenset({excluded_name})
    )

    # Rejection via search_tools
    search_result = await provider.execute_tool(
        SEARCH_TOOLS_TOOL_NAME, {"query": excluded_name}, _context()
    )
    assert isinstance(search_result, ToolResult)
    data = cast("dict[str, Any]", search_result.data)
    assert data["match_count"] == 0

    # Rejection via call_tool
    call_result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {"name": excluded_name, "arguments_json": "{}"},
        _context(),
    )
    assert isinstance(call_result, ToolResult)
    assert f"Tool '{excluded_name}' is not available in voice sessions." in (
        call_result.text or ""
    )
    assert CALLS == []


class _ProviderWithAnMcpServer(LocalToolsProvider):
    """Reports every ``ha_`` tool as belonging to one MCP server."""

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return [
            replace(descriptor, mcp_server_id="homeassistant")
            if descriptor.name.startswith("ha_")
            else descriptor
            for descriptor in await super().get_tool_descriptors()
        ]


def _skill_provider(
    # ast-grep-ignore: no-dict-any - get_note result data for a test fixture
    note_data: dict[str, Any],
    *,
    long_tool_description: str = "Run a Home Assistant action on an entity.",
) -> LiveMetaToolsProvider:
    async def get_note() -> ToolResult:
        return ToolResult(data=note_data)

    local_provider = _ProviderWithAnMcpServer(
        registrations=[
            _registration(
                "get_note", description="Read a note.", implementation=get_note
            ),
            _registration("list_notes", description="List the household's notes."),
            _registration(
                "call_home_assistant_action",
                description=long_tool_description,
                properties={
                    "entity_id": {"type": "string", "description": "Entity to act on."}
                },
            ),
            _registration(
                "list_home_assistant_entities", description="Find entities by name."
            ),
            _registration("ha_call_read_tool", description="Raw Home Assistant read."),
        ]
    )
    view = OnDemandToolsView(
        wrapped_provider=local_provider,
        on_demand_tool_names={
            "call_home_assistant_action",
            "list_home_assistant_entities",
            "ha_call_read_tool",
        },
    )
    return LiveMetaToolsProvider(view)


# ast-grep-ignore: no-dict-any - get_note result data for assertions
async def _load_skill(provider: LiveMetaToolsProvider) -> dict[str, Any]:
    result = await provider.execute_tool("get_note", {}, _context())
    assert isinstance(result, ToolResult)
    assert isinstance(result.data, dict)
    return result.data


@pytest.mark.asyncio
async def test_skill_hands_over_its_tools_instead_of_activating_them() -> None:
    provider = _skill_provider({
        "content": "Find the entity, then call the action.",
        "activate_tools": ["call_home_assistant_action"],
    })

    data = await _load_skill(provider)

    assert data["content"] == "Find the entity, then call the action."
    assert "activate_tools" not in data
    [entry] = data[SKILL_TOOLS_KEY]
    assert entry["name"] == "call_home_assistant_action"
    assert entry["already_declared"] is False
    assert "entity_id" in entry["parameters"]["properties"]


@pytest.mark.asyncio
async def test_skill_tool_can_be_run_with_the_schema_it_handed_over() -> None:
    provider = _skill_provider({
        "content": "Skill body.",
        "activate_tools": ["call_home_assistant_action"],
    })
    [entry] = (await _load_skill(provider))[SKILL_TOOLS_KEY]

    await provider.execute_tool(
        CALL_TOOL_TOOL_NAME,
        {"name": entry["name"], "arguments_json": '{"entity_id": "vacuum.s8"}'},
        _context(),
    )

    assert CALLS == [("called", {"entity_id": "vacuum.s8"})]


@pytest.mark.asyncio
async def test_skill_loaded_through_call_tool_also_hands_over_its_tools() -> None:
    provider = _skill_provider({
        "content": "Skill body.",
        "activate_tools": ["call_home_assistant_action"],
    })

    result = await provider.execute_tool(
        CALL_TOOL_TOOL_NAME, {"name": "get_note"}, _context()
    )

    assert isinstance(result, ToolResult)
    assert isinstance(result.data, dict)
    assert SKILL_TOOLS_KEY in result.data


@pytest.mark.asyncio
async def test_skill_marks_a_tool_that_is_already_declared() -> None:
    provider = _skill_provider({
        "content": "Skill body.",
        "activate_tools": ["list_notes"],
    })

    [entry] = (await _load_skill(provider))[SKILL_TOOLS_KEY]

    assert entry["already_declared"] is True
    assert "parameters" not in entry


@pytest.mark.asyncio
async def test_skill_summarises_a_whole_mcp_server() -> None:
    provider = _skill_provider({
        "content": "Skill body.",
        "activate_mcp_servers": ["homeassistant"],
    })

    data = await _load_skill(provider)

    assert "activate_mcp_servers" not in data
    [entry] = data[SKILL_TOOLS_KEY]
    assert entry["name"] == "ha_call_read_tool"
    assert "parameters" not in entry


@pytest.mark.asyncio
async def test_skill_past_the_schema_budget_summarises_the_rest() -> None:
    provider = _skill_provider(
        {
            "content": "Skill body.",
            "activate_tools": [
                "list_home_assistant_entities",
                "call_home_assistant_action",
            ],
        },
        long_tool_description="x" * SKILL_SCHEMA_BUDGET_CHARS,
    )

    first, second = (await _load_skill(provider))[SKILL_TOOLS_KEY]

    assert "parameters" in first
    assert "parameters" not in second


@pytest.mark.asyncio
async def test_skill_omits_a_tool_the_session_may_not_run() -> None:
    provider = _skill_provider({
        "content": "Skill body.",
        "activate_tools": ["no_such_tool"],
    })

    data = await _load_skill(provider)

    assert "activate_tools" not in data
    assert SKILL_TOOLS_KEY not in data


@pytest.mark.asyncio
async def test_ordinary_note_is_returned_unchanged() -> None:
    note = {"title": "Pool", "content": "Chlorine on Sundays."}

    assert await _load_skill(_skill_provider(note)) == note
