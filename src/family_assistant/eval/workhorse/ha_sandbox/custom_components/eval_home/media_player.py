"""Media players: power, playback, volume, source and what is playing.

Playing media records what was asked for as the current media; there is no
library behind the player, so browsing shows an empty one.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from typing import TYPE_CHECKING

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.util.enum import try_parse_enum

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_DEFAULT_FEATURES = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.BROWSE_MEDIA
)
_DERIVED = ("entity_picture_local", "media_position_updated_at", "volume_step")


class WorldMediaPlayer(WorldEntity, MediaPlayerEntity):
    def setup_world(self, attributes: Attributes) -> None:
        # The recorded picture is a proxy URL with a production token.
        self._attr_entity_picture = None
        self._attr_state = try_parse_enum(MediaPlayerState, self.world_state)
        self._attr_device_class = try_parse_enum(
            MediaPlayerDeviceClass, attributes.text("device_class")
        )
        self._attr_volume_level = attributes.number("volume_level")
        self._attr_is_volume_muted = attributes.flag("is_volume_muted")
        self._attr_media_content_id = attributes.text("media_content_id")
        self._attr_media_content_type = attributes.text("media_content_type")
        self._attr_media_title = attributes.text("media_title")
        self._attr_media_artist = attributes.text("media_artist")
        self._attr_media_album_name = attributes.text("media_album_name")
        self._attr_media_album_artist = attributes.text("media_album_artist")
        self._attr_media_duration = attributes.integer("media_duration")
        self._attr_media_position = attributes.integer("media_position")
        self._attr_media_series_title = attributes.text("media_series_title")
        self._attr_media_channel = attributes.text("media_channel")
        self._attr_app_id = attributes.text("app_id")
        self._attr_app_name = attributes.text("app_name")
        self._attr_source = attributes.text("source")
        self._attr_source_list = attributes.texts("source_list")
        self._attr_sound_mode = attributes.text("sound_mode")
        self._attr_sound_mode_list = attributes.texts("sound_mode_list")
        self._attr_shuffle = attributes.flag("shuffle")
        self._attr_repeat = try_parse_enum(RepeatMode, attributes.text("repeat"))
        self._attr_group_members = attributes.texts("group_members")
        for derived in _DERIVED:
            attributes.take(derived)
        features = attributes.integer("supported_features")
        self._attr_supported_features = (
            MediaPlayerEntityFeature(features)
            if features is not None
            else _DEFAULT_FEATURES
        )

    def _set_state(self, state: MediaPlayerState) -> None:
        self._attr_state = state
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        self._set_state(MediaPlayerState.ON)

    async def async_turn_off(self) -> None:
        self._set_state(MediaPlayerState.OFF)

    async def async_media_play(self) -> None:
        self._set_state(MediaPlayerState.PLAYING)

    async def async_media_pause(self) -> None:
        self._set_state(MediaPlayerState.PAUSED)

    async def async_media_stop(self) -> None:
        self._set_state(MediaPlayerState.IDLE)

    async def async_media_next_track(self) -> None:
        self.async_write_ha_state()

    async def async_media_previous_track(self) -> None:
        self.async_write_ha_state()

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: object
    ) -> None:
        self._attr_media_content_type = media_type
        self._attr_media_content_id = media_id
        self._attr_media_title = media_id
        self._set_state(MediaPlayerState.PLAYING)

    async def async_set_volume_level(self, volume: float) -> None:
        self._attr_volume_level = volume
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        self._attr_is_volume_muted = mute
        self.async_write_ha_state()

    async def async_select_source(self, source: str) -> None:
        self._attr_source = source
        self.async_write_ha_state()

    async def async_select_sound_mode(self, sound_mode: str) -> None:
        self._attr_sound_mode = sound_mode
        self.async_write_ha_state()

    async def async_set_shuffle(self, shuffle: bool) -> None:
        self._attr_shuffle = shuffle
        self.async_write_ha_state()

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        self._attr_repeat = repeat
        self.async_write_ha_state()

    async def async_media_seek(self, position: float) -> None:
        self._attr_media_position = int(position)
        self.async_write_ha_state()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="",
            media_content_type=MediaType.APPS,
            title=self.name if isinstance(self.name, str) else self.entity_id,
            can_play=False,
            can_expand=True,
            children=[],
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "media_player", WorldMediaPlayer))
