"""The ``plugins`` config block and how a profile selects instances from it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field

from family_assistant.plugins.home_assistant.config import (
    HomeAssistantConfig,  # noqa: TC001 - Pydantic needs at runtime
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# The instance a profile gets when it doesn't name one.
DEFAULT_INSTANCE = "default"

# `default_profile_settings.processing_config` keys that became settings of the
# default Home Assistant instance, by their new name.
_LEGACY_HOME_ASSISTANT_KEYS: dict[str, str] = {
    "home_assistant_api_url": "api_url",
    "home_assistant_token": "token",
    "home_assistant_context_template": "context_template",
    "home_assistant_verify_ssl": "verify_ssl",
}


class PluginsConfig(BaseModel):
    """Configured instances of each plugin, keyed by instance name.

    Each field is named for a plugin id in ``family_assistant.plugins.registry``.
    """

    model_config = ConfigDict(extra="forbid")

    home_assistant: dict[str, HomeAssistantConfig] = Field(default_factory=dict)

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


def migrate_legacy_home_assistant_settings(
    # ast-grep-ignore: no-dict-any - raw config data before validation
    data: dict[str, Any],
) -> None:
    """Move pre-plugin Home Assistant settings to the default instance, in place.

    Deployed config written before Home Assistant became a plugin keeps working
    until it is rewritten; the new location wins where both are set.
    """
    processing = (data.get("default_profile_settings") or {}).get(
        "processing_config"
    ) or {}
    legacy = {
        new_key: processing.pop(old_key)
        for old_key, new_key in _LEGACY_HOME_ASSISTANT_KEYS.items()
        if old_key in processing
    }
    legacy = {key: value for key, value in legacy.items() if value is not None}
    if not legacy:
        return
    logger.warning(
        "default_profile_settings.processing_config.home_assistant_* is deprecated; "
        "move %s to plugins.home_assistant.default.",
        ", ".join(sorted(legacy)),
    )
    plugins = data.setdefault("plugins", {})
    instances = plugins.setdefault("home_assistant", {})
    instances[DEFAULT_INSTANCE] = {**legacy, **(instances.get(DEFAULT_INSTANCE) or {})}
