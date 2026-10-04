"""A running Home Assistant plugin instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.plugins.base import PluginInstance, PluginProfileContext
from family_assistant.plugins.home_assistant.context import (
    HomeAssistantContextProvider,
)
from family_assistant.plugins.home_assistant.events import HomeAssistantSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.context_providers import ContextProvider
    from family_assistant.events.sources import EventSource
    from family_assistant.plugins.home_assistant.client import (
        HomeAssistantClientWrapper,
    )


class HomeAssistantInstance(PluginInstance):
    """One Home Assistant server: its client, context template and event source."""

    def __init__(
        self,
        client: HomeAssistantClientWrapper,
        *,
        context_template: str | None = None,
        events: bool = False,
    ) -> None:
        self.client = client
        self._context_template = context_template
        self._event_source = HomeAssistantSource(client) if events else None

    @property
    def context_template(self) -> str | None:
        """The operator's home status template, if one is configured."""
        return self._context_template

    def context_providers(
        self, profile: PluginProfileContext
    ) -> Sequence[ContextProvider]:
        if not self._context_template:
            return ()
        return (
            HomeAssistantContextProvider(
                client=self.client,
                context_template=self._context_template,
                prompts=profile.prompts,
            ),
        )

    def event_sources(self) -> Sequence[EventSource]:
        return () if self._event_source is None else (self._event_source,)
