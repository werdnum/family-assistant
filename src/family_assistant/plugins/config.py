"""The ``plugins`` config block and how a profile selects instances from it."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, Field

from family_assistant.plugins.home_assistant.config import (
    HomeAssistantConfig,  # noqa: TC001 - Pydantic needs at runtime
)
from family_assistant.plugins.reolink.config import (
    ReolinkConfig,  # noqa: TC001 - Pydantic needs at runtime
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# The instance a profile gets when it doesn't name one.
DEFAULT_INSTANCE = "default"


class PluginsConfig(BaseModel):
    """Configured instances of each plugin, keyed by instance name.

    Each field is named for a plugin id in ``family_assistant.plugins.registry``.
    """

    model_config = ConfigDict(extra="forbid")

    home_assistant: dict[str, HomeAssistantConfig] = Field(default_factory=dict)
    reolink: dict[str, ReolinkConfig] = Field(default_factory=dict)

    def instances(self, plugin_id: str) -> Mapping[str, BaseModel]:
        """The configured instances of one plugin."""
        return cast("Mapping[str, BaseModel]", getattr(self, plugin_id))


def resolve_profile_plugins(
    plugins: PluginsConfig, selection: Mapping[str, str | None]
) -> dict[str, str]:
    """Which instance of each plugin a profile uses, keyed by plugin id.

    Every plugin with a ``default`` instance serves the profile unless its
    ``selection`` names another instance, or ``None`` to go without.
    """
    resolved = {
        plugin_id: DEFAULT_INSTANCE
        for plugin_id in PluginsConfig.model_fields
        if DEFAULT_INSTANCE in plugins.instances(plugin_id)
    }
    for plugin_id, instance_name in selection.items():
        if plugin_id not in PluginsConfig.model_fields:
            msg = (
                f"Unknown plugin {plugin_id!r}; known plugins: "
                f"{', '.join(sorted(PluginsConfig.model_fields))}"
            )
            raise ValueError(msg)
        if instance_name is None:
            resolved.pop(plugin_id, None)
            continue
        if instance_name not in plugins.instances(plugin_id):
            msg = f"Plugin {plugin_id!r} has no configured instance {instance_name!r}"
            raise ValueError(msg)
        resolved[plugin_id] = instance_name
    return resolved
