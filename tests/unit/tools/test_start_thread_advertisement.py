"""``start_thread`` is offered only where it could succeed.

A tool that refuses every call is a dead end advertised as a capability, so the
switch takes it away whatever a profile's policy grants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.assistant import Assistant
from family_assistant.config_loader import load_config
from family_assistant.tools.catalog import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.config_models import AppConfig

pytestmark = pytest.mark.no_db

TOOL = "start_thread"


def _config(tmp_path: Path, *, enabled: bool) -> AppConfig:
    config = load_config(
        defaults_file_path="defaults.yaml",
        config_file_path=str(tmp_path / "missing-config.yaml"),
    )
    config.conversation_threads.enabled = enabled
    return config


async def _advertised(config: AppConfig) -> bool:
    profile = next(p for p in config.service_profiles if p.id == "default_assistant")
    registration = next(item for item in LOCAL_TOOL_REGISTRATIONS if item.name == TOOL)
    assistant = Assistant(config)
    assistant.root_tools_provider = CompositeToolsProvider(
        providers=[LocalToolsProvider(registrations=[registration])]
    )
    provider, _ = await assistant._build_profile_tools_provider(
        profile, delegation_sink_classes={}, tool_call_reviewer=None
    )
    definitions = await provider.get_tool_definitions(can_confirm=False)
    return TOOL in {item["function"]["name"] for item in definitions}


@pytest.mark.asyncio
async def test_threads_disabled_withholds_the_tool(tmp_path: Path) -> None:
    assert await _advertised(_config(tmp_path, enabled=False)) is False


@pytest.mark.asyncio
async def test_threads_enabled_grants_the_tool(tmp_path: Path) -> None:
    assert await _advertised(_config(tmp_path, enabled=True)) is True
