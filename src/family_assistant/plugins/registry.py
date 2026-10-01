"""The plugins this build knows about."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from family_assistant.plugins.home_assistant.plugin import HOME_ASSISTANT_PLUGIN
from family_assistant.plugins.reolink.plugin import REOLINK_PLUGIN
from family_assistant.plugins.trino.plugin import TRINO_PLUGIN
from family_assistant.tools.metadata import join_tool_registrations

if TYPE_CHECKING:
    from family_assistant.plugins.base import Plugin
    from family_assistant.tools.metadata import ToolRegistration

PLUGINS: tuple[Plugin[Any, Any], ...] = (
    HOME_ASSISTANT_PLUGIN,
    REOLINK_PLUGIN,
    TRINO_PLUGIN,
)

PLUGINS_BY_ID: dict[str, Plugin[Any, Any]] = {plugin.id: plugin for plugin in PLUGINS}


def plugin_tool_registrations() -> list[ToolRegistration]:
    """Every plugin's tools, in registry order."""
    return join_tool_registrations(*(plugin.tools for plugin in PLUGINS))
