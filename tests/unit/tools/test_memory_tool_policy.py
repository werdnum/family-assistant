"""Which shipped profiles advertise `propose_memory_edits`.

Slice 2 of docs/design/conversation-memory.md grants the tool alongside
`add_or_update_note` in the two profiles whose note writes are not
label-confined, and slice 3 adds the curator. A grant is not the whole answer,
though: writing memory requires reading it, so the tool is withheld from a
profile with `memory_read` off wherever a profile's tool policy is assembled.
What is pinned here is therefore the effective set advertised by the
assistant's profile tools provider, rather than the rules that feed it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.mcp import MCPToolsProvider

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig, ServiceProfile

pytestmark = pytest.mark.no_db

MEMORY_TOOL = "propose_memory_edits"


def _config(tmp_path: Path) -> AppConfig:
    return load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )


def _profile(config: AppConfig, profile_id: str) -> ServiceProfile:
    return next(p for p in config.service_profiles if p.id == profile_id)


async def _holds_memory_tool(config: AppConfig, profile_id: str) -> bool:
    """Whether the assistant advertises the memory tool for this profile."""
    profile = _profile(config, profile_id)
    registration = next(
        item for item in LOCAL_TOOL_REGISTRATIONS if item.name == MEMORY_TOOL
    )
    assistant = Assistant(config)
    local_provider = LocalToolsProvider(registrations=[registration])
    assistant._root_local_registrations = [registration]
    assistant._root_mcp_provider = MCPToolsProvider(mcp_server_configs={})
    assistant.root_tools_provider = CompositeToolsProvider(
        providers=[local_provider, assistant._root_mcp_provider]
    )
    provider, _ = await assistant._build_profile_tools_provider(
        profile, delegation_sink_classes={}, tool_call_reviewer=None
    )
    definitions = await provider.get_tool_definitions(can_confirm=False)
    return MEMORY_TOOL in {item["function"]["name"] for item in definitions}


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
@pytest.mark.asyncio
async def test_the_shipped_foreground_profiles_hold_the_tool(
    tmp_path: Path, profile_id: str
) -> None:
    """They read memory, so "remember this" reaches memory rather than a note."""
    config = _config(tmp_path)

    assert _profile(config, profile_id).processing_config.memory_read is True
    assert await _holds_memory_tool(config, profile_id) is True


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
@pytest.mark.asyncio
async def test_the_same_profile_loses_the_tool_when_it_stops_reading_memory(
    tmp_path: Path, profile_id: str
) -> None:
    """The grant stays; `memory_read` is the whole of the difference.

    A deployment that turns memory off for a profile leaves the tool's grant in
    the shipped policy untouched, and the tool must still disappear: it would
    refuse every call it received, and `add_or_update_note` -- which works -- is
    what a "remember this" should reach instead.
    """
    config = _config(tmp_path)
    _profile(config, profile_id).processing_config.memory_read = False

    assert await _holds_memory_tool(config, profile_id) is False


@pytest.mark.parametrize("profile_id", ["default_assistant", "complex_tasks"])
@pytest.mark.asyncio
async def test_the_master_switch_takes_the_tool_from_a_reading_profile(
    tmp_path: Path, profile_id: str
) -> None:
    """`memory_config.enabled: false` reaches the foreground, not just the sweep.

    It is documented as turning the whole mechanism off for every profile at
    once, so a profile that still carries `memory_read: true` must lose the tool
    anyway -- otherwise a deployment that opted out of memory would still be
    offered a way to write it.
    """
    config = _config(tmp_path)
    config.memory_config.enabled = False

    assert _profile(config, profile_id).processing_config.memory_read is True
    assert await _holds_memory_tool(config, profile_id) is False


@pytest.mark.asyncio
async def test_the_curator_holds_the_tool(tmp_path: Path) -> None:
    """The shipped profile whose whole job is curating memory."""
    config = _config(tmp_path)

    assert _profile(config, "memory_curator").processing_config.memory_read is True
    assert await _holds_memory_tool(config, "memory_curator") is True


@pytest.mark.parametrize("profile_id", ["event_handler", "ops_automation"])
@pytest.mark.asyncio
async def test_label_confined_profiles_do_not_get_the_memory_tool(
    tmp_path: Path, profile_id: str
) -> None:
    """Their note writes carry a required label a memory note cannot also carry."""
    assert await _holds_memory_tool(_config(tmp_path), profile_id) is False
