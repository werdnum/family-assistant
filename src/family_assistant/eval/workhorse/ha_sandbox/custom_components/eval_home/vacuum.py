"""Robot vacuums: start, stop, dock and vendor commands change what it is doing.

The household's vacuum is driven mostly by ``send_command`` with the vendor's
own commands (room and zone cleaning). A command that starts a clean puts it
to cleaning, one that docks it sends it home; any other is accepted unchanged.
There is no clock running a clean to completion: it cleans until told otherwise.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_DEFAULT_FEATURES = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.FAN_SPEED
    | VacuumEntityFeature.SEND_COMMAND
    | VacuumEntityFeature.LOCATE
    | VacuumEntityFeature.CLEAN_SPOT
    | VacuumEntityFeature.STATE
)


def _activity_for_command(command: str) -> VacuumActivity | None:
    lowered = command.lower()
    if "charge" in lowered or "dock" in lowered or "home" in lowered:
        return VacuumActivity.RETURNING
    if "pause" in lowered:
        return VacuumActivity.PAUSED
    if "stop" in lowered:
        return VacuumActivity.IDLE
    if "clean" in lowered or "start" in lowered or "goto" in lowered:
        return VacuumActivity.CLEANING
    return None


class WorldVacuum(WorldEntity, StateVacuumEntity):
    def setup_world(self, attributes: Attributes) -> None:
        self._attr_activity = try_parse_enum(VacuumActivity, self.world_state)
        self._attr_fan_speed_list = attributes.texts("fan_speed_list") or []
        self._attr_fan_speed = attributes.text("fan_speed")
        self._attr_battery_level = attributes.integer("battery_level")
        attributes.take("battery_icon")
        features = attributes.integer("supported_features")
        self._attr_supported_features = (
            VacuumEntityFeature(features) if features is not None else _DEFAULT_FEATURES
        )

    def _become(self, activity: VacuumActivity) -> None:
        self._attr_activity = activity
        self.async_write_ha_state()

    async def async_start(self) -> None:
        self._become(VacuumActivity.CLEANING)

    async def async_pause(self) -> None:
        self._become(VacuumActivity.PAUSED)

    async def async_stop(self, **kwargs: object) -> None:
        self._become(VacuumActivity.IDLE)

    async def async_return_to_base(self, **kwargs: object) -> None:
        self._become(VacuumActivity.RETURNING)

    async def async_clean_spot(self, **kwargs: object) -> None:
        self._become(VacuumActivity.CLEANING)

    async def async_locate(self, **kwargs: object) -> None:
        """The vacuum would announce itself; nothing changes."""

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: object) -> None:
        self._attr_fan_speed = fan_speed
        self.async_write_ha_state()

    async def async_send_command(
        self,
        command: str,
        params: dict[str, object] | list[object] | None = None,
        **kwargs: object,
    ) -> None:
        activity = _activity_for_command(command)
        if activity is not None:
            self._become(activity)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "vacuum", WorldVacuum))
