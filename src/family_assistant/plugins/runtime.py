"""Started plugin instances, and the slice of them each profile sees."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from family_assistant.plugins.config import resolve_profile_plugins
from family_assistant.plugins.registry import PLUGINS_BY_ID

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.events.sources import EventSource
    from family_assistant.plugins.base import (
        PluginInstance,
        PluginStartupContext,
    )
    from family_assistant.plugins.config import PluginsConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProfilePlugins:
    """The plugin instances one profile selected, one per plugin at most."""

    instances: Sequence[PluginInstance] = field(default_factory=tuple)

    def get[InstanceT: PluginInstance](
        self, instance_type: type[InstanceT]
    ) -> InstanceT | None:
        """The profile's instance of the plugin whose instances are ``instance_type``."""
        for instance in self.instances:
            if isinstance(instance, instance_type):
                return instance
        return None


def withheld_profile_tools(
    config: PluginsConfig, selection: Mapping[str, str | None]
) -> frozenset[str]:
    """Tools a profile with this ``plugins`` selection cannot use.

    See ``Plugin.withheld_from_profile``.
    """
    resolved = resolve_profile_plugins(config, selection)
    withheld: set[str] = set()
    for plugin_id, plugin in PLUGINS_BY_ID.items():
        instance_name = resolved.get(plugin_id)
        instance_config = (
            None
            if instance_name is None
            else config.instances(plugin_id)[instance_name]
        )
        withheld |= plugin.withheld_from_profile(instance_config)
    return frozenset(withheld)


class PluginRuntime:
    """Every configured plugin instance, started once and shared by profiles."""

    def __init__(self, config: PluginsConfig) -> None:
        self._config = config
        self._instances: dict[tuple[str, str], PluginInstance] = {}
        for plugin_id, plugin in PLUGINS_BY_ID.items():
            for instance_name, instance_config in config.instances(plugin_id).items():
                logger.info("Starting plugin %s instance %r", plugin_id, instance_name)
                instance = plugin.start(instance_name, instance_config)
                if instance is not None:
                    self._instances[plugin_id, instance_name] = instance

    def for_profile(self, selection: Mapping[str, str | None]) -> ProfilePlugins:
        """The instances a profile with this ``plugins`` selection uses."""
        resolved = resolve_profile_plugins(self._config, selection)
        return ProfilePlugins(
            tuple(
                self._instances[key]
                for key in resolved.items()
                if key in self._instances
            )
        )

    def event_sources(self) -> dict[str, EventSource]:
        """Every instance's event sources, keyed by source id."""
        sources: dict[str, EventSource] = {}
        for (plugin_id, instance_name), instance in self._instances.items():
            for source in instance.event_sources():
                if source.source_id in sources:
                    msg = (
                        f"Plugin {plugin_id} instance {instance_name!r} supplies event "
                        f"source {source.source_id!r}, which another instance already "
                        "supplies; enable events on only one of them."
                    )
                    raise ValueError(msg)
                sources[source.source_id] = source
        return sources

    async def on_startup(self, context: PluginStartupContext) -> None:
        """Run every instance's startup hook, logging any that fails."""
        for (plugin_id, instance_name), instance in self._instances.items():
            try:
                await instance.on_startup(context)
            except Exception:
                logger.exception(
                    "Startup hook of plugin %s instance %r failed",
                    plugin_id,
                    instance_name,
                )

    async def close(self) -> None:
        """Close every instance."""
        for instance in self._instances.values():
            await instance.close()
