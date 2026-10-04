"""The Home Assistant plugin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from family_assistant.plugins.base import Plugin
from family_assistant.plugins.home_assistant.client import create_home_assistant_client
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.tools import (
    GET_HOME_STATUS_TOOL_NAME,
    HOME_ASSISTANT_TOOLS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.tools.metadata import ToolRegistration

logger = logging.getLogger(__name__)


class HomeAssistantPlugin(Plugin[HomeAssistantConfig, HomeAssistantInstance]):
    """Home Assistant tools, context template and state-change events."""

    id: ClassVar[str] = "home_assistant"
    config_model = HomeAssistantConfig
    tools = HOME_ASSISTANT_TOOLS

    def served_tools(
        self, configs: Mapping[str, HomeAssistantConfig]
    ) -> Sequence[ToolRegistration]:
        """Every tool, except ``get_home_status`` when no instance has a template.

        The status tool renders an instance's ``context_template``; without one
        there is nothing for it to show.
        """
        if any(config.context_template for config in configs.values()):
            return self.tools
        return tuple(
            registration
            for registration in self.tools
            if registration.name != GET_HOME_STATUS_TOOL_NAME
        )

    def withheld_from_profile(
        self, config: HomeAssistantConfig | None
    ) -> frozenset[str]:
        """``get_home_status`` unless the profile's instance has a template.

        ``served_tools`` serves it when any instance has one; a profile using
        another instance, or none, would only ever get an error from it.
        """
        if config is not None and config.context_template:
            return frozenset()
        return frozenset({GET_HOME_STATUS_TOOL_NAME})

    def start(
        self, instance_name: str, config: HomeAssistantConfig
    ) -> HomeAssistantInstance | None:
        # Deployment templates commonly set HOMEASSISTANT_API_KEY alone, or to an
        # empty string, without meaning to enable Home Assistant.
        if (
            not config.api_url
            or config.token is None
            or not config.token.get_secret_value()
        ):
            logger.warning(
                "Home Assistant instance %r not started: it needs both api_url and "
                "token (HOMEASSISTANT_URL and HOMEASSISTANT_API_KEY for the default "
                "instance).",
                instance_name,
            )
            return None
        return HomeAssistantInstance(
            create_home_assistant_client(
                api_url=config.api_url,
                token=config.token.get_secret_value(),
                verify_ssl=config.verify_ssl,
            ),
            context_template=config.context_template,
            events=config.events,
        )


HOME_ASSISTANT_PLUGIN = HomeAssistantPlugin()
