"""A running Trino plugin instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.plugins.base import PluginInstance

if TYPE_CHECKING:
    from family_assistant.plugins.trino.client import TrinoClient
    from family_assistant.plugins.trino.config import TrinoConfig


class TrinoInstance(PluginInstance):
    """One coordinator, queried as one user."""

    def __init__(self, config: TrinoConfig, client: TrinoClient) -> None:
        self.config = config
        self.client = client

    async def close(self) -> None:
        await self.client.close()
