"""A running Reolink plugin instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.plugins.base import PluginInstance

if TYPE_CHECKING:
    from family_assistant.plugins.reolink.protocol import CameraBackend


class ReolinkInstance(PluginInstance):
    """One set of cameras, reached through a camera backend."""

    def __init__(self, backend: CameraBackend) -> None:
        self.backend = backend

    async def close(self) -> None:
        await self.backend.close()
