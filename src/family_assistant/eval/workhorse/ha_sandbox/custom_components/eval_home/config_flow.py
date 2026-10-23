"""One config entry per household integration, created on import at startup."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class EvalHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def is_matching(self, other_flow: EvalHomeConfigFlow) -> bool:
        return False

    # ast-grep-ignore: no-dict-any - Home Assistant's ConfigFlow signature
    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        integration = str(import_data["integration"])
        await self.async_set_unique_id(integration)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=integration, data={"integration": integration}
        )
