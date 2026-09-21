"""Build the deployment-effective local tool registry.

The source registry is customized at startup before it is exposed: deployment
configuration changes some schemas, and unavailable OAuth-backed tools are
removed. Consumers that describe the running tool surface must use the same
construction path or they can silently accept calls the deployment could not
have made.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING

from family_assistant.services.oauth_integration_state import (
    filter_oauth_tool_registrations,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_METADATA_BY_NAME,
    TOOLS_DEFINITION,
    _scan_user_docs,
    build_local_tool_registrations,
)

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.services.oauth_integration_state import OAuthIntegrationState
    from family_assistant.tools import ToolDefinition, ToolRegistration

logger = logging.getLogger(__name__)


def build_effective_local_tool_definitions(config: AppConfig) -> list[ToolDefinition]:
    """Return local definitions customized for one deployment."""
    available_doc_files = _scan_user_docs()
    formatted_doc_list = ", ".join(available_doc_files) or "None"
    definitions = copy.deepcopy(TOOLS_DEFINITION)

    for definition in definitions:
        function = definition.get("function", {})
        if function.get("name") != "get_user_documentation_content":
            continue
        try:
            function["description"] = function["description"].format(
                available_doc_files=formatted_doc_list
            )
        except KeyError as exc:
            logger.error(
                "Failed to format doc tool description during tool setup: %s", exc
            )
        break

    available_agents = config.ai_worker_config.available_agents
    for definition in definitions:
        function = definition.get("function", {})
        if function.get("name") != "spawn_worker":
            continue
        agent_parameter = (
            function.get("parameters", {}).get("properties", {}).get("agent")
        )
        if agent_parameter:
            agent_parameter["enum"] = available_agents
            logger.debug("Updated spawn_worker agent enum to %s", available_agents)
        break

    return definitions


def build_effective_local_tool_registrations(
    config: AppConfig,
    google_integration_state: OAuthIntegrationState,
) -> list[ToolRegistration]:
    """Return the root local registrations the deployment actually serves."""
    registrations = build_local_tool_registrations(
        definitions=build_effective_local_tool_definitions(config),
        implementations=AVAILABLE_FUNCTIONS,
        metadata_by_name=LOCAL_TOOL_METADATA_BY_NAME,
    )
    return filter_oauth_tool_registrations(registrations, google_integration_state)
