"""The contract every plugin implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from family_assistant.context_providers import ContextProvider
    from family_assistant.events.sources import EventSource
    from family_assistant.storage.database import Database
    from family_assistant.tools.metadata import ToolRegistration
    from family_assistant.tools.types import ToolExecutionContext

    # The task worker's handler signature: an execution context and the task's
    # payload, whose shape each task type defines.
    type TaskHandler = Callable[[ToolExecutionContext, Any], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PluginProfileContext:
    """What a plugin instance may know about the profile it is serving."""

    profile_id: str
    prompts: Mapping[str, str]
    timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class PluginStartupContext:
    """What a plugin instance may use once the application has started."""

    database: Database
    shared_workspace_path: Path


class PluginInstance:
    """One configured instance of a plugin, started once and shared by profiles.

    Subclasses override only what their integration supplies; the defaults
    contribute nothing.
    """

    def context_providers(
        self,
        profile: PluginProfileContext,
    ) -> Sequence[ContextProvider]:
        """Context providers this instance adds to a profile that selects it."""
        return ()

    def event_sources(self) -> Sequence[EventSource]:
        """Event sources this instance runs; each ``source_id`` must be unique."""
        return ()

    async def on_startup(self, context: PluginStartupContext) -> None:
        """Run once the task worker pool is up, e.g. to seed recurring tasks.

        Runs in the background, so startup does not wait on it. A failure is
        logged and does not stop other instances' hooks.
        """
        return

    async def close(self) -> None:
        """Release anything the instance holds open."""
        return


class Plugin[ConfigT: BaseModel, InstanceT: PluginInstance](ABC):
    """An integration: its config model, its tools, and how to start it.

    ``tools`` are registered whether or not the plugin is configured, so a
    profile's tool policy can name them and the tool inventory is the same in
    every deployment. A tool whose plugin has no instance for the profile
    reports that when called.

    ``task_handlers`` are registered with every task worker whether or not the
    plugin is configured, so a task queued while it was configured still runs
    after it is removed. A handler that needs an instance finds it through the
    execution context, as a tool does.
    """

    id: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    tools: ClassVar[Sequence[ToolRegistration]] = ()
    task_handlers: ClassVar[Mapping[str, TaskHandler]] = {}

    def served_tools(
        self, configs: Mapping[str, ConfigT]
    ) -> Sequence[ToolRegistration]:
        """The tools a deployment with these configured instances offers.

        Every tool by default. A plugin may withhold tools that cannot work
        with its current configuration, or adjust their definitions to it.
        """
        _ = configs
        return self.tools

    @abstractmethod
    def start(self, instance_name: str, config: ConfigT) -> InstanceT | None:
        """Build the runtime instance for one configured entry.

        ``None`` means the entry is incomplete and the plugin logged why; the
        instance is left out and profiles selecting it run without it.
        """
