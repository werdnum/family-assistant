"""Build the deployment-effective local tool registry.

The source registry is customized at startup before it is exposed: deployment
configuration changes some schemas, plugins serve the tools their configuration
supports, the weather tool is served only where weather is configured, and
unavailable OAuth-backed tools are removed. Consumers that
describe the running tool surface must use the same construction path or they
can silently accept calls the deployment could not have made.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from typing import TYPE_CHECKING

from family_assistant.plugins.registry import PLUGINS
from family_assistant.services.oauth_integration_state import (
    filter_oauth_tool_registrations,
)
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS, _scan_user_docs
from family_assistant.tools.weather import (
    GET_WEATHER_FORECAST_TOOL_NAME,
    bind_weather_service,
)
from family_assistant.weather import is_weather_configured

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.services.oauth_integration_state import OAuthIntegrationState
    from family_assistant.tools import ToolRegistration
    from family_assistant.weather import WeatherService

logger = logging.getLogger(__name__)

_DOCUMENTATION_TOOL_NAME = "get_user_documentation_content"


def _with_documentation_inventory(registration: ToolRegistration) -> ToolRegistration:
    """The documentation tool, with the files it can read listed."""
    available_doc_files = _scan_user_docs()
    formatted_doc_list = ", ".join(available_doc_files) or "None"
    definition = copy.deepcopy(registration.definition)
    function = definition["function"]
    try:
        function["description"] = function["description"].format(
            available_doc_files=formatted_doc_list
        )
    except KeyError as exc:
        logger.error("Failed to format doc tool description during tool setup: %s", exc)
    return dataclasses.replace(registration, definition=definition)


def build_effective_local_tool_registrations(
    config: AppConfig,
    google_integration_state: OAuthIntegrationState,
    weather_service: WeatherService | None = None,
) -> list[ToolRegistration]:
    """Return the root local registrations the deployment actually serves.

    Each plugin serves the tools its configured instances support (see
    ``Plugin.served_tools``), in the catalogue's order.

    ``get_weather_forecast`` is withheld unless the configuration names a
    WillyWeather key and location, and is served by ``weather_service`` when
    one is given. Callers that only describe the tool surface may omit it.
    """
    plugin_tool_names = {
        registration.name for plugin in PLUGINS for registration in plugin.tools
    }
    served_plugin_tools = {
        registration.name: registration
        for plugin in PLUGINS
        for registration in plugin.served_tools(config.plugins.instances(plugin.id))
    }
    registrations: list[ToolRegistration] = []
    for registration in LOCAL_TOOL_REGISTRATIONS:
        if registration.name in plugin_tool_names:
            served = served_plugin_tools.get(registration.name)
            if served is not None:
                registrations.append(served)
        elif registration.name == _DOCUMENTATION_TOOL_NAME:
            registrations.append(_with_documentation_inventory(registration))
        elif registration.name == GET_WEATHER_FORECAST_TOOL_NAME:
            if not is_weather_configured(config):
                continue
            registrations.append(
                registration
                if weather_service is None
                else bind_weather_service(registration, weather_service)
            )
        else:
            registrations.append(registration)
    return filter_oauth_tool_registrations(registrations, google_integration_state)
