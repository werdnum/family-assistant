"""Climate: modes, setpoints and fan settings, validated as Home Assistant does.

Home Assistant itself rejects a mode the unit does not list, a setpoint out of
range, or a call the unit's features do not support, so those failures look
as they would at home. What the unit is doing (``hvac_action``) follows the
mode it is put in.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_ACTION_OF_MODE = {
    HVACMode.OFF: HVACAction.OFF,
    HVACMode.HEAT: HVACAction.HEATING,
    HVACMode.COOL: HVACAction.COOLING,
    HVACMode.DRY: HVACAction.DRYING,
    HVACMode.FAN_ONLY: HVACAction.FAN,
}


class WorldClimate(WorldEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def setup_world(self, attributes: Attributes) -> None:
        mode = try_parse_enum(HVACMode, self.world_state)
        modes = [
            parsed
            for name in attributes.texts("hvac_modes") or []
            if (parsed := try_parse_enum(HVACMode, name)) is not None
        ]
        if mode is not None and mode not in modes:
            modes.append(mode)
        if HVACMode.OFF not in modes:
            modes.append(HVACMode.OFF)
        self._attr_hvac_modes = modes
        self._attr_hvac_mode = mode
        self._last_on_mode = next((m for m in modes if m != HVACMode.OFF), None)
        self._attr_hvac_action = try_parse_enum(
            HVACAction, attributes.text("hvac_action")
        )
        self._tracks_action = self._attr_hvac_action is not None
        self._attr_current_temperature = attributes.number("current_temperature")
        self._attr_target_temperature = attributes.number("temperature")
        self._attr_target_temperature_high = attributes.number("target_temp_high")
        self._attr_target_temperature_low = attributes.number("target_temp_low")
        self._attr_target_temperature_step = attributes.number("target_temp_step")
        minimum = attributes.number("min_temp")
        maximum = attributes.number("max_temp")
        if minimum is not None:
            self._attr_min_temp = minimum
        if maximum is not None:
            self._attr_max_temp = maximum
        self._attr_current_humidity = attributes.number("current_humidity")
        self._attr_target_humidity = attributes.number("humidity")
        self._attr_fan_modes = attributes.texts("fan_modes")
        self._attr_fan_mode = attributes.text("fan_mode")
        self._attr_preset_modes = attributes.texts("preset_modes")
        self._attr_preset_mode = attributes.text("preset_mode")
        self._attr_swing_modes = attributes.texts("swing_modes")
        self._attr_swing_mode = attributes.text("swing_mode")
        for derived in ("min_humidity", "max_humidity"):
            attributes.take(derived)
        recorded = attributes.integer("supported_features")
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if recorded is not None:
            features |= ClimateEntityFeature(recorded)
        else:
            if self._attr_target_temperature is not None:
                features |= ClimateEntityFeature.TARGET_TEMPERATURE
            if self._attr_fan_modes:
                features |= ClimateEntityFeature.FAN_MODE
            if self._attr_preset_modes:
                features |= ClimateEntityFeature.PRESET_MODE
            if self._attr_swing_modes:
                features |= ClimateEntityFeature.SWING_MODE
        self._attr_supported_features = features

    def _follow_mode(self) -> None:
        mode = self._attr_hvac_mode
        if mode is not None and mode != HVACMode.OFF:
            self._last_on_mode = mode
        if self._tracks_action and mode is not None:
            self._attr_hvac_action = _ACTION_OF_MODE.get(mode, HVACAction.IDLE)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        self._follow_mode()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(self._last_on_mode or self.hvac_modes[0])

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: object) -> None:
        for key, attribute in (
            (ATTR_TEMPERATURE, "_attr_target_temperature"),
            (ATTR_TARGET_TEMP_HIGH, "_attr_target_temperature_high"),
            (ATTR_TARGET_TEMP_LOW, "_attr_target_temperature_low"),
        ):
            value = kwargs.get(key)
            if isinstance(value, (int, float)):
                setattr(self, attribute, float(value))
        mode = kwargs.get(ATTR_HVAC_MODE)
        if isinstance(mode, str):
            self._attr_hvac_mode = HVACMode(mode)
            self._follow_mode()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._attr_preset_mode = preset_mode
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        self._attr_swing_mode = swing_mode
        self.async_write_ha_state()

    async def async_set_humidity(self, humidity: int) -> None:
        self._attr_target_humidity = humidity
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "climate", WorldClimate))
