"""Event entities: the last event recorded, and the types they can fire."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities, parse_datetime

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldEvent(WorldEntity, EventEntity):
    def setup_world(self, attributes: Attributes) -> None:
        last_type = attributes.text("event_type")
        types = attributes.texts("event_types") or []
        if last_type is not None and last_type not in types:
            types.append(last_type)
        self._attr_event_types = types
        self._attr_device_class = try_parse_enum(
            EventDeviceClass, attributes.text("device_class")
        )
        fired = parse_datetime(self.world_state)
        if fired is not None and last_type is not None:
            # EventEntity keeps its last event privately and restores it only
            # from its own restore cache; the recorded event is set the same way.
            self._EventEntity__last_event_triggered = fired
            self._EventEntity__last_event_type = last_type
            self._EventEntity__last_event_attributes = attributes.rest()
        for key in list(attributes.rest()):
            attributes.take(key)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "event", WorldEvent))
