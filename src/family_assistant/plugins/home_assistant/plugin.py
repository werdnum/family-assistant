"""The Home Assistant plugin."""

from __future__ import annotations

from typing import ClassVar

from family_assistant.plugins.base import Plugin
from family_assistant.plugins.home_assistant.client import create_home_assistant_client
from family_assistant.plugins.home_assistant.config import HomeAssistantConfig
from family_assistant.plugins.home_assistant.instance import HomeAssistantInstance
from family_assistant.plugins.home_assistant.tools import HOME_ASSISTANT_TOOLS


class HomeAssistantPlugin(Plugin[HomeAssistantConfig, HomeAssistantInstance]):
    """Home Assistant tools, context template and state-change events."""

    id: ClassVar[str] = "home_assistant"
    config_model = HomeAssistantConfig
    tools = HOME_ASSISTANT_TOOLS

    def start(
        self, instance_name: str, config: HomeAssistantConfig
    ) -> HomeAssistantInstance:
        if not config.api_url or config.token is None:
            msg = (
                f"Home Assistant instance {instance_name!r} needs both api_url and "
                "token (or the HOMEASSISTANT_URL and HOMEASSISTANT_API_KEY "
                "environment variables for the default instance)."
            )
            raise ValueError(msg)
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
