"""Numbers: setting a value within the recorded range makes it current."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities, parse_number

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldNumber(WorldEntity, NumberEntity):
    def setup_world(self, attributes: Attributes) -> None:
        value = parse_number(self.world_state)
        minimum = attributes.number("min")
        maximum = attributes.number("max")
        self._attr_native_min_value = (
            minimum if minimum is not None else min(0.0, value or 0.0)
        )
        self._attr_native_max_value = (
            maximum if maximum is not None else max(100.0, value or 0.0)
        )
        self._attr_native_step = attributes.number("step") or 1.0
        self._attr_mode = try_parse_enum(NumberMode, attributes.text("mode")) or (
            NumberMode.AUTO
        )
        self._attr_device_class = try_parse_enum(
            NumberDeviceClass, attributes.text("device_class")
        )
        self._attr_native_unit_of_measurement = attributes.text("unit_of_measurement")
        self._attr_native_value = value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "number", WorldNumber))
