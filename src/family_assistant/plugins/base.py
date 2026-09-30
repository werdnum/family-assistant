"""The contract every plugin implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from zoneinfo import ZoneInfo

    from family_assistant.context_providers import ContextProvider
    from family_assistant.events.sources import EventSource
    from family_assistant.tools.metadata import ToolRegistration


@dataclass(frozen=True, slots=True)
class PluginProfileContext:
    """What a plugin instance may know about the profile it is serving."""

    profile_id: str
    prompts: Mapping[str, str]
    timezone: ZoneInfo


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

    async def close(self) -> None:
        """Release anything the instance holds open."""
        return


class Plugin[ConfigT: BaseModel, InstanceT: PluginInstance](ABC):
    """An integration: its config model, its tools, and how to start it.

    ``tools`` are registered whether or not the plugin is configured, so a
    profile's tool policy can name them and the tool inventory is the same in
    every deployment. A tool whose plugin has no instance for the profile
    reports that when called.
    """

    id: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    tools: ClassVar[Sequence[ToolRegistration]] = ()

    @abstractmethod
    def start(self, instance_name: str, config: ConfigT) -> InstanceT:
        """Build the runtime instance for one configured entry."""
