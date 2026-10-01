"""The AI worker plugin."""

from __future__ import annotations

import copy
import dataclasses
from typing import TYPE_CHECKING, ClassVar

from family_assistant.plugins.ai_workers.config import AIWorkersConfig
from family_assistant.plugins.ai_workers.instance import AIWorkersInstance
from family_assistant.plugins.ai_workers.tasks import (
    WORKER_TASK_CLEANUP_TASK_TYPE,
    handle_worker_task_cleanup,
)
from family_assistant.plugins.ai_workers.tools import AI_WORKER_TOOLS
from family_assistant.plugins.base import Plugin

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.plugins.base import TaskHandler
    from family_assistant.tools.metadata import ToolRegistration


class AIWorkersPlugin(Plugin[AIWorkersConfig, AIWorkersInstance]):
    """Isolated coding agents working in the shared workspace."""

    id: ClassVar[str] = "ai_workers"
    config_model = AIWorkersConfig
    tools = AI_WORKER_TOOLS
    task_handlers: ClassVar[Mapping[str, TaskHandler]] = {
        WORKER_TASK_CLEANUP_TASK_TYPE: handle_worker_task_cleanup,
    }

    def start(self, instance_name: str, config: AIWorkersConfig) -> AIWorkersInstance:
        _ = instance_name
        return AIWorkersInstance(config)

    def served_tools(
        self, configs: Mapping[str, AIWorkersConfig]
    ) -> Sequence[ToolRegistration]:
        """No tools without a sandbox; with one, ``agent`` offers its agents.

        With no instance there is no backend to run, cancel or report on a
        worker, so offering the tools would only invite calls that cannot
        succeed.
        """
        if not configs:
            return ()
        # PluginsConfig allows at most one sandbox.
        (config,) = configs.values()
        agents = list(config.available_agents)
        return tuple(
            _with_agent_choices(registration, agents)
            if registration.name == "spawn_worker"
            else registration
            for registration in self.tools
        )


def _with_agent_choices(
    registration: ToolRegistration, agents: list[str]
) -> ToolRegistration:
    definition = copy.deepcopy(registration.definition)
    properties = definition["function"]["parameters"].get("properties", {})
    properties["agent"]["enum"] = agents
    return dataclasses.replace(registration, definition=definition)


AI_WORKERS_PLUGIN = AIWorkersPlugin()
