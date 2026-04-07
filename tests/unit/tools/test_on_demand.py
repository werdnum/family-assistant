"""Tests for on-demand tool activation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import (
    MCPServerLoadingEntry,
    ToolLoadingEntry,
    ToolsConfig,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    extract_tool_summary,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import (
    OnDemandAwareToolsProvider,
    OnDemandCatalogEntry,
    OnDemandToolCatalog,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


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
    """Test ToolsConfig helper methods for eager/on-demand splitting."""

    def test_plain_strings_all_eager(self) -> None:
        tc = ToolsConfig(enable_local_tools=["a", "b", "c"])
        assert tc.get_all_tool_names() == {"a", "b", "c"}
        assert tc.get_eager_tool_names() == {"a", "b", "c"}
        assert tc.get_on_demand_tool_names() == set()

    def test_mixed_eager_and_on_demand(self) -> None:
        tc = ToolsConfig(
            enable_local_tools=[
                "eager_tool",
                ToolLoadingEntry(name="lazy_tool", loading="on_demand"),
            ]
        )
        assert tc.get_all_tool_names() == {"eager_tool", "lazy_tool"}
        assert tc.get_eager_tool_names() == {"eager_tool"}
        assert tc.get_on_demand_tool_names() == {"lazy_tool"}

    def test_none_means_unfiltered(self) -> None:
        tc = ToolsConfig()
        assert tc.get_all_tool_names() is None
        assert tc.get_eager_tool_names() is None
        assert tc.get_on_demand_tool_names() == set()

    def test_from_dict_yaml_format(self) -> None:
        """Test that YAML-style dicts are parsed correctly by Pydantic."""
        tc = ToolsConfig(
            enable_local_tools=[
                "tool_a",
                ToolLoadingEntry(name="tool_b", loading="on_demand"),
            ]
        )
        assert tc.get_eager_tool_names() == {"tool_a"}
        assert tc.get_on_demand_tool_names() == {"tool_b"}

    def test_mcp_server_ids_mixed(self) -> None:
        tc = ToolsConfig(
            enable_mcp_server_ids=[
                "time",
                MCPServerLoadingEntry(id="homeassistant", loading="on_demand"),
            ]
        )
        assert tc.get_all_mcp_server_ids() == ["time", "homeassistant"]
        assert tc.get_eager_mcp_server_ids() == ["time"]
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


class TestOnDemandAwareToolsProvider:
    """Test OnDemandAwareToolsProvider behavior."""

    @pytest.mark.asyncio
    async def test_eager_tools_returned_on_demand_excluded(self) -> None:
        provider = _make_provider(["eager_a", "eager_b", "lazy_c", "lazy_d"])
        on_demand = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_c", "lazy_d"},
        )

        definitions = await on_demand.get_tool_definitions()
        names = {d["function"]["name"] for d in definitions}
        assert names == {"eager_a", "eager_b", "activate_tools"}

    @pytest.mark.asyncio
    async def test_on_demand_catalog_contains_only_on_demand(self) -> None:
        provider = _make_provider(["eager_a", "lazy_b"])
        on_demand = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_b"},
        )

        catalog = await on_demand.get_on_demand_catalog()
        assert len(catalog.entries) == 1
        assert catalog.entries[0].name == "lazy_b"

    @pytest.mark.asyncio
    async def test_activate_by_name(self) -> None:
        provider = _make_provider(["eager_a", "lazy_b", "lazy_c"])
        on_demand = OnDemandAwareToolsProvider(
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
        on_demand = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names={"camera_tool", "automation_tool"},
        )

        result = await on_demand.activate_tools(search="camera")
        assert result.newly_activated == frozenset({"camera_tool"})
        assert len(result.definitions) == 1
        assert result.definitions[0]["function"]["name"] == "camera_tool"

    @pytest.mark.asyncio
    async def test_on_demand_tools_still_executable(self) -> None:
        """On-demand tools should be executable even before activation."""

        provider = _make_provider(["eager_a", "lazy_b"])
        on_demand = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_b"},
        )

        mock_db = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test",
            user_name="test",
            turn_id=None,
            interface_type="test",
            db_context=mock_db,
            processing_service=MagicMock(),
            clock=MagicMock(),
            home_assistant_client=MagicMock(),
            event_sources={},
            attachment_registry=MagicMock(),
            camera_backend=MagicMock(),
            timezone=ZoneInfo("UTC"),
        )

        result = await on_demand.execute_tool("lazy_b", {}, context)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_has_on_demand_tools(self) -> None:
        provider = _make_provider(["a"])
        on_demand_yes = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names={"a"},
        )
        on_demand_no = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names=set(),
        )
        assert on_demand_yes.has_on_demand_tools() is True
        assert on_demand_no.has_on_demand_tools() is False

    @pytest.mark.asyncio
    async def test_all_descriptors_returned(self) -> None:
        """get_tool_descriptors should return ALL descriptors (eager + on-demand)."""
        provider = _make_provider(["eager_a", "lazy_b"])
        on_demand = OnDemandAwareToolsProvider(
            wrapped_provider=provider,
            on_demand_tool_names={"lazy_b"},
        )

        descriptors = await on_demand.get_tool_descriptors()
        names = {d.name for d in descriptors}
        assert names == {"eager_a", "lazy_b"}

    @pytest.mark.asyncio
    async def test_activation_state_is_turn_local(self) -> None:
        """Provider holds no activation state; callers pass activated per turn."""
        provider = _make_provider(["eager_a", "lazy_b"])
        on_demand = OnDemandAwareToolsProvider(
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
