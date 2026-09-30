"""Started plugin instances, and the slice of them each profile sees."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from family_assistant.plugins.config import resolve_profile_plugins
from family_assistant.plugins.registry import PLUGINS_BY_ID

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.context_providers import ContextProvider
    from family_assistant.events.sources import EventSource
    from family_assistant.plugins.base import PluginInstance, PluginProfileContext
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

    def context_providers(self, profile: PluginProfileContext) -> list[ContextProvider]:
        """Every context provider the selected instances add for ``profile``."""
        return [
            provider
            for instance in self.instances
            for provider in instance.context_providers(profile)
        ]


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

    async def close(self) -> None:
        """Close every instance."""
        for instance in self._instances.values():
            await instance.close()
