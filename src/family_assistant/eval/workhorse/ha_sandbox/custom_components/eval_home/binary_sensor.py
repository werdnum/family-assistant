"""Binary sensors."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldBinarySensor(WorldEntity, BinarySensorEntity):
    def setup_world(self, attributes: Attributes) -> None:
        raw_class = attributes.text("device_class")
        self._attr_device_class = try_parse_enum(BinarySensorDeviceClass, raw_class)
        if raw_class is not None and self._attr_device_class is None:
            attributes.keep("device_class", raw_class)
        self._attr_is_on = (
            None if self.world_state is None else self.world_state == "on"
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "binary_sensor", WorldBinarySensor))
