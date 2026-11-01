"""Lights: turning on sets brightness and colour the way the light supports.

Home Assistant converts what a caller asks for (``brightness_pct``, a colour
name, a kelvin value) into the light's own colour modes before the entity sees
it, so the entity only records the result and the mode it puts the light in.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_WHITE,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_DERIVED = (
    "color_temp",
    "min_mireds",
    "max_mireds",
    "rgbw_color",
    "rgbww_color",
)


def _pair(values: tuple[float, ...] | None) -> tuple[float, float] | None:
    return (values[0], values[1]) if values is not None and len(values) == 2 else None


def _triple(values: tuple[float, ...] | None) -> tuple[int, int, int] | None:
    if values is None or len(values) != 3:
        return None
    return (int(values[0]), int(values[1]), int(values[2]))


class WorldLight(WorldEntity, LightEntity):
    def setup_world(self, attributes: Attributes) -> None:
        modes = {
            mode
            for name in attributes.texts("supported_color_modes") or []
            if (mode := try_parse_enum(ColorMode, name)) is not None
        }
        brightness = attributes.integer("brightness")
        if not modes:
            modes = {
                ColorMode.BRIGHTNESS if brightness is not None else ColorMode.ONOFF
            }
        self._attr_supported_color_modes = modes
        self._attr_is_on = (
            None if self.world_state is None else self.world_state == "on"
        )
        color_mode = try_parse_enum(ColorMode, attributes.text("color_mode"))
        if self._attr_is_on and (color_mode is None or color_mode not in modes):
            color_mode = sorted(modes)[0]
        self._attr_color_mode = color_mode if self._attr_is_on else None
        self._attr_brightness = brightness
        self._attr_hs_color = _pair(attributes.numbers("hs_color"))
        self._attr_xy_color = _pair(attributes.numbers("xy_color"))
        self._attr_rgb_color = _triple(attributes.numbers("rgb_color"))
        self._attr_color_temp_kelvin = attributes.integer("color_temp_kelvin")
        self._attr_min_color_temp_kelvin = attributes.integer("min_color_temp_kelvin")
        self._attr_max_color_temp_kelvin = attributes.integer("max_color_temp_kelvin")
        if ColorMode.COLOR_TEMP in modes:
            self._attr_min_color_temp_kelvin = self._attr_min_color_temp_kelvin or 2000
            self._attr_max_color_temp_kelvin = self._attr_max_color_temp_kelvin or 6500
        self._attr_effect = attributes.text("effect")
        self._attr_effect_list = attributes.texts("effect_list")
        features = attributes.integer("supported_features") or 0
        if self._attr_effect_list:
            features |= LightEntityFeature.EFFECT
        self._attr_supported_features = LightEntityFeature(features)
        for derived in _DERIVED:
            attributes.take(derived)

    def _choose_mode(self, wanted: ColorMode) -> None:
        if wanted in (self._attr_supported_color_modes or set()):
            self._attr_color_mode = wanted

    async def async_turn_on(self, **kwargs: object) -> None:
        modes = self._attr_supported_color_modes or {ColorMode.ONOFF}
        self._attr_is_on = True
        if self._attr_color_mode is None:
            self._attr_color_mode = sorted(modes)[0]
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if isinstance(brightness, int):
            self._attr_brightness = brightness
        elif self._attr_brightness is None and modes != {ColorMode.ONOFF}:
            self._attr_brightness = 255
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if isinstance(kelvin, int):
            self._attr_color_temp_kelvin = kelvin
            self._choose_mode(ColorMode.COLOR_TEMP)
        hs = kwargs.get(ATTR_HS_COLOR)
        if isinstance(hs, tuple):
            self._attr_hs_color = (float(hs[0]), float(hs[1]))
            self._choose_mode(ColorMode.HS)
        xy = kwargs.get(ATTR_XY_COLOR)
        if isinstance(xy, tuple):
            self._attr_xy_color = (float(xy[0]), float(xy[1]))
            self._choose_mode(ColorMode.XY)
        rgb = kwargs.get(ATTR_RGB_COLOR)
        if isinstance(rgb, tuple):
            self._attr_rgb_color = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            self._choose_mode(ColorMode.RGB)
        if isinstance(kwargs.get(ATTR_RGBW_COLOR), tuple):
            self._choose_mode(ColorMode.RGBW)
        if isinstance(kwargs.get(ATTR_RGBWW_COLOR), tuple):
            self._choose_mode(ColorMode.RGBWW)
        if ATTR_WHITE in kwargs:
            self._choose_mode(ColorMode.WHITE)
        effect = kwargs.get(ATTR_EFFECT)
        if isinstance(effect, str):
            self._attr_effect = effect
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "light", WorldLight))
