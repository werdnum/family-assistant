"""Tests for on-demand tool activation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from family_assistant.config_models import ToolsConfig
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolDescriptor,
    ToolRegistration,
    ToolTag,
    extract_tool_summary,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import (
    OnDemandCatalogEntry,
    OnDemandToolCatalog,
    OnDemandToolsView,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


def _make_tool_def(name: str, description: str = "A test tool.") -> ToolDefinition:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


async def _noop_tool(**_kwargs: Any) -> str:  # noqa: ANN401 - test helper
    return "ok"


def _make_provider(tool_names: list[str]) -> LocalToolsProvider:
    """Create a LocalToolsProvider with simple tools."""
    registrations = [
        ToolRegistration(
            definition=_make_tool_def(name, f"Description of {name}."),
            implementation=_noop_tool,
            metadata=make_local_tool_metadata([
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            ]),
        )
        for name in tool_names
    ]
    return LocalToolsProvider(registrations=registrations)


class TestToolsConfig:
    """Test ToolsConfig helper methods for on-demand catalog hints."""

    def test_default_has_no_on_demand_hints(self) -> None:
        tc = ToolsConfig()
        assert tc.get_on_demand_tool_names() == set()
        assert tc.get_on_demand_mcp_server_ids() == []

    def test_on_demand_local_tools(self) -> None:
        tc = ToolsConfig(
            on_demand_local_tools=["lazy_tool", "another_lazy_tool"],
        )
        assert tc.get_on_demand_tool_names() == {"lazy_tool", "another_lazy_tool"}

    def test_on_demand_local_tools_from_yaml_list(self) -> None:
        tc = ToolsConfig.model_validate({
            "on_demand_local_tools": ["tool_b"],
        })
        assert tc.get_on_demand_tool_names() == {"tool_b"}

    def test_on_demand_mcp_server_ids(self) -> None:
        tc = ToolsConfig.model_validate({
            "on_demand_mcp_server_ids": ["homeassistant"],
        })
        assert tc.get_on_demand_mcp_server_ids() == ["homeassistant"]


class TestExtractToolSummary:
    """Test extract_tool_summary function."""

    def test_first_sentence(self) -> None:
        defn = _make_tool_def(
            "test", "Create a new automation. Supports various triggers."
        )
        assert extract_tool_summary(defn) == "Create a new automation"

    def test_truncation(self) -> None:
        defn = _make_tool_def("test", "A" * 200)
        summary = extract_tool_summary(defn)
        assert len(summary) <= 120
        assert summary.endswith("...")

    def test_empty_description(self) -> None:
        defn = _make_tool_def("fallback", "")
        assert extract_tool_summary(defn) == "fallback"


class TestOnDemandToolCatalog:
    """Test OnDemandToolCatalog rendering."""

    def test_empty_catalog(self) -> None:
        catalog = OnDemandToolCatalog(entries=[])
        assert not catalog.render_for_system_prompt()

    def test_catalog_rendering(self) -> None:
        catalog = OnDemandToolCatalog(
            entries=[
                OnDemandCatalogEntry(name="tool_a", summary="Does A"),
                OnDemandCatalogEntry(name="tool_b", summary="Does B"),
            ]
        )
        text = catalog.render_for_system_prompt()
        assert "## On-Demand Tools" in text
        assert "- **tool_a**: Does A" in text
        assert "- **tool_b**: Does B" in text
        assert "activate_tools" in text


class TestOnDemandToolsView:
    """Test OnDemandToolsView behavior."""

    @pytest.mark.asyncio
    async def test_eager_tools_returned_on_demand_excluded(self) -> None:
        provider = _make_provider(["eager_a", "eager_b", "lazy_c", "lazy_d"])
        on_demand = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_c", "lazy_d"},
        )

        definitions = await on_demand.get_tool_definitions()
        names = {d["function"]["name"] for d in definitions}
        assert names == {"eager_a", "eager_b", "activate_tools"}

    @pytest.mark.asyncio
    async def test_on_demand_catalog_contains_only_on_demand(self) -> None:
        provider = _make_provider(["eager_a", "lazy_b"])
        on_demand = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_b"},
        )

        catalog = await on_demand.get_on_demand_catalog()
        assert len(catalog.entries) == 1
        assert catalog.entries[0].name == "lazy_b"

    @pytest.mark.asyncio
    async def test_activate_by_name(self) -> None:
        provider = _make_provider(["eager_a", "lazy_b", "lazy_c"])
        on_demand = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_b", "lazy_c"},
        )

        result = await on_demand.activate_tools(names=["lazy_b"])
        assert result.newly_activated == frozenset({"lazy_b"})
        assert len(result.definitions) == 1
        assert result.definitions[0]["function"]["name"] == "lazy_b"

        # Thread the turn-local activated set through subsequent calls.
        activated = result.newly_activated
        definitions = await on_demand.get_tool_definitions(activated=activated)
        names = {d["function"]["name"] for d in definitions}
        assert names == {"eager_a", "lazy_b", "activate_tools"}

        # And catalog should only have lazy_c
        catalog = await on_demand.get_on_demand_catalog(activated=activated)
        assert len(catalog.entries) == 1
        assert catalog.entries[0].name == "lazy_c"

    @pytest.mark.asyncio
    async def test_activate_by_search(self) -> None:
        provider = _make_provider(["notes_tool", "camera_tool", "automation_tool"])
        on_demand = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"camera_tool", "automation_tool"},
        )

        result = await on_demand.activate_tools(search="camera")
        assert result.newly_activated == frozenset({"camera_tool"})
        assert len(result.definitions) == 1
        assert result.definitions[0]["function"]["name"] == "camera_tool"

    @pytest.mark.asyncio
    async def test_has_on_demand_tools(self) -> None:
        provider = _make_provider(["a"])
        on_demand_yes = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"a"},
        )
        on_demand_no = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names=set(),
        )
        assert on_demand_yes.has_on_demand_tools() is True
        assert on_demand_no.has_on_demand_tools() is False

    @pytest.mark.asyncio
    async def test_activation_state_is_turn_local(self) -> None:
        """Provider holds no activation state; callers pass activated per turn."""
        provider = _make_provider(["eager_a", "lazy_b"])
        on_demand = OnDemandToolsView(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_b"},
        )

        result = await on_demand.activate_tools(names=["lazy_b"])
        assert result.newly_activated == frozenset({"lazy_b"})

        # Turn A sees the activated tool when it passes its turn-local set.
        turn_a_defs = await on_demand.get_tool_definitions(
            activated=result.newly_activated
        )
        assert {d["function"]["name"] for d in turn_a_defs} == {"eager_a", "lazy_b"}

        # Turn B (no activated set) still sees lazy_b as on-demand; the
        # provider state was not mutated by turn A.
        turn_b_defs = await on_demand.get_tool_definitions()
        assert {d["function"]["name"] for d in turn_b_defs} == {
            "eager_a",
            "activate_tools",
        }


class _StubMCPDescriptorProvider:
    """Minimal ToolsProvider whose descriptors carry mcp_server_id values.

    Used to exercise the OnDemandToolsView's MCP server expansion
    behavior, which the LocalToolsProvider helper does not cover because
    LocalToolsProvider produces local-origin descriptors with no server id.
    """

    def __init__(self, descriptors: list[ToolDescriptor]) -> None:
        self._descriptors = descriptors
        self._definitions = [descriptor.definition for descriptor in descriptors]

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return list(self._definitions)

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return list(self._descriptors)

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        for descriptor in self._descriptors:
            if descriptor.name == name:
                return descriptor
        return None

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        del arguments, context, call_id
        return f"executed {name}"

    async def close(self) -> None:
        return None


def _make_mcp_descriptor(name: str, server_id: str | None) -> ToolDescriptor:
    definition = _make_tool_def(name, f"Description of {name}.")
    return ToolDescriptor(
        name=name,
        definition=definition,
        tags=frozenset(),
        origin="mcp",
        mcp_server_id=server_id,
        summary=name,
    )


class TestOnDemandMCPServerExpansion:
    """MCP-server expansion when activating an on-demand MCP tool."""

    @pytest.mark.asyncio
    async def test_activating_one_unlocks_other_on_demand_tools_on_same_server(
        self,
    ) -> None:
        descriptors = [
            _make_mcp_descriptor("ha_call_service", "homeassistant"),
            _make_mcp_descriptor("ha_get_state", "homeassistant"),
            _make_mcp_descriptor("ha_get_history", "homeassistant"),
            _make_mcp_descriptor("brave_search", "brave"),
        ]
        wrapped = _StubMCPDescriptorProvider(descriptors)
        on_demand = OnDemandToolsView(
            wrapped_provider=wrapped,
            on_demand_tool_names=set(),
            on_demand_mcp_server_ids={"homeassistant"},
        )

        result = await on_demand.activate_tools(names=["ha_call_service"])

        # All three on-demand tools from the homeassistant server are unlocked
        # in a single activation, even though only one was requested by name.
        assert result.newly_activated == frozenset({
            "ha_call_service",
            "ha_get_state",
            "ha_get_history",
        })
        # The brave server is unrelated and stays untouched.
        assert "brave_search" not in result.newly_activated

    @pytest.mark.asyncio
    async def test_eager_tools_on_same_server_are_not_re_activated(self) -> None:
        """Eager tools on the same server must not be reported as newly activated.

        Eager tools were never on-demand to begin with, so reporting them in
        the activation result would lead to duplicate definitions in the LLM
        tool list.
        """
        descriptors = [
            _make_mcp_descriptor("ha_lazy_one", "homeassistant"),
            _make_mcp_descriptor("ha_lazy_two", "homeassistant"),
            _make_mcp_descriptor("ha_eager", "homeassistant"),
        ]
        wrapped = _StubMCPDescriptorProvider(descriptors)
        on_demand = OnDemandToolsView(
            wrapped_provider=wrapped,
            # Only ha_lazy_one and ha_lazy_two are on-demand. ha_eager lives on
            # the same MCP server but is eager — server-id-based on-demand is
            # NOT configured for homeassistant.
            on_demand_tool_names={"ha_lazy_one", "ha_lazy_two"},
        )

        result = await on_demand.activate_tools(names=["ha_lazy_one"])

        assert result.newly_activated == frozenset({"ha_lazy_one", "ha_lazy_two"})
        assert "ha_eager" not in result.newly_activated
        # The eager tool's definition is not duplicated in the activation result.
        returned_names = {d["function"]["name"] for d in result.definitions}
        assert "ha_eager" not in returned_names


class TestOnDemandActivateToolsCollision:
    """The synthetic activate_tools name must be reserved by the provider."""

    @pytest.mark.asyncio
    async def test_wrapped_provider_with_real_activate_tools_raises(self) -> None:
        """A real tool named ``activate_tools`` would be shadowed by the meta-tool.

        Refuse to wrap such a provider so the collision surfaces at setup
        rather than silently hiding the real tool at runtime.
        """
        descriptors = [
            _make_mcp_descriptor("activate_tools", "homeassistant"),
        ]
        wrapped = _StubMCPDescriptorProvider(descriptors)
        on_demand = OnDemandToolsView(
            wrapped_provider=wrapped,
            on_demand_tool_names=set(),
            on_demand_mcp_server_ids={"homeassistant"},
        )

        with pytest.raises(ValueError, match="reserved for the on-demand meta-tool"):
            await on_demand.get_tool_definitions()
