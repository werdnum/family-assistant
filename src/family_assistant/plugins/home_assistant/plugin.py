"""The Home Assistant plugin."""

from __future__ import annotations

import logging
from typing import ClassVar

from family_assistant.plugins.base import Plugin
from family_assistant.plugins.home_assistant.client import create_home_assistant_client
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.tools import HOME_ASSISTANT_TOOLS

logger = logging.getLogger(__name__)


class HomeAssistantPlugin(Plugin[HomeAssistantConfig, HomeAssistantInstance]):
    """Home Assistant tools, context template and state-change events."""

    id: ClassVar[str] = "home_assistant"
    config_model = HomeAssistantConfig
    tools = HOME_ASSISTANT_TOOLS

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
