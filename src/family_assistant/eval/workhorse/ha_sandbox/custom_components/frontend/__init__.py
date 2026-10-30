"""Stand in for Home Assistant's frontend, which the sandbox does not serve.

Home Assistant treats the frontend as critical and falls back to recovery
mode when it cannot load, and the frontend needs the separately packaged web
UI. Nothing in the sandbox is used through a browser, so this integration of
the same domain takes its place and does nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import homeassistant.helpers.config_validation as cv

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

DOMAIN = "frontend"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True
