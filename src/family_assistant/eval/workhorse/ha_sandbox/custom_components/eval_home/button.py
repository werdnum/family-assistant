"""Buttons: pressing one records when, as Home Assistant itself does."""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities, parse_datetime

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


class WorldButton(WorldEntity, ButtonEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_device_class = try_parse_enum(
            ButtonDeviceClass, attributes.text("device_class")
        )
        pressed = parse_datetime(self.world_state)
        if pressed is not None:
            # ButtonEntity keeps its last press privately and restores it only
            # from its own restore cache; the recorded press is set the same way.
            self._ButtonEntity__last_pressed_isoformat = pressed.isoformat()

    async def async_press(self) -> None:
        """Home Assistant records the press; nothing else happens in the sandbox."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "button", WorldButton))
