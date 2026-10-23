"""Remotes: on, off and activities; commands are accepted and logged."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.remote import (
    ATTR_ACTIVITY,
    RemoteEntity,
    RemoteEntityFeature,
)

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldRemote(WorldEntity, RemoteEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_is_on = (
            None if self.world_state is None else self.world_state == "on"
        )
        self._attr_activity_list = attributes.texts("activity_list")
        self._attr_current_activity = attributes.text("current_activity")
        self._attr_supported_features = RemoteEntityFeature(
            attributes.integer("supported_features") or 0
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = True
        activity = kwargs.get(ATTR_ACTIVITY)
        if isinstance(activity, str):
            self._attr_current_activity = activity
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_send_command(
        self, command: Iterable[str], **kwargs: object
    ) -> None:
        """The command would go to the device; it is in the service log."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "remote", WorldRemote))
