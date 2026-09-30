"""The plugins this build knows about."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from family_assistant.plugins.ai_workers.plugin import AI_WORKERS_PLUGIN
from family_assistant.plugins.home_assistant.plugin import HOME_ASSISTANT_PLUGIN
from family_assistant.tools.metadata import join_tool_registrations

if TYPE_CHECKING:
    from family_assistant.plugins.base import Plugin, TaskHandler
    from family_assistant.tools.metadata import ToolRegistration

PLUGINS: tuple[Plugin[Any, Any], ...] = (HOME_ASSISTANT_PLUGIN, AI_WORKERS_PLUGIN)

PLUGINS_BY_ID: dict[str, Plugin[Any, Any]] = {plugin.id: plugin for plugin in PLUGINS}


def plugin_tool_registrations() -> list[ToolRegistration]:
    """Every plugin's tools, in registry order."""
    return join_tool_registrations(*(plugin.tools for plugin in PLUGINS))


def plugin_task_handlers() -> dict[str, TaskHandler]:
    """Every plugin's task handlers, keyed by task type."""
    handlers: dict[str, TaskHandler] = {}
    for plugin in PLUGINS:
        for task_type, handler in plugin.task_handlers.items():
            if task_type in handlers:
                msg = f"Plugin {plugin.id} registers task type {task_type!r} again"
                raise ValueError(msg)
            handlers[task_type] = handler
    return handlers
