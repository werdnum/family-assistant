"""Per-turn context rendered from a Home Assistant template."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant_api.errors import HomeassistantAPIError

from family_assistant.context_providers import ContextProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.plugins.home_assistant.client import (
        HomeAssistantClientWrapper,
    )

logger = logging.getLogger(__name__)


class HomeAssistantContextProvider(ContextProvider):
    """Provides context by rendering a Jinja2 template via Home Assistant."""

    def __init__(
        self,
        client: HomeAssistantClientWrapper,
        context_template: str,
        prompts: Mapping[str, str],
    ) -> None:
        self._ha_client = client
        self._context_template = context_template
        self._prompts = prompts

    @property
    def name(self) -> str:
        return "home_assistant"

    def _format_rendered_template(self, rendered_template: str | None) -> list[str]:
        if rendered_template and rendered_template.strip():
            header = self._prompts.get("home_assistant_context_header", "").strip()
            full_context = (
                f"{header}\n{rendered_template.strip()}"
                if header
                else rendered_template.strip()
            )
            logger.debug(
                f"[{self.name}] Successfully rendered Home Assistant template."
            )
            return [full_context.strip()]

        logger.info(
            f"[{self.name}] Rendered Home Assistant template was empty or whitespace only."
        )
        empty_message = self._prompts.get("home_assistant_template_empty", "").strip()
        return [empty_message] if empty_message else []

    def _api_error_fragments(self) -> list[str]:
        error_message = self._prompts.get(
            "home_assistant_api_error", "Error retrieving data from Home Assistant."
        ).strip()
        return [error_message] if error_message else []

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        """Render the configured template through the Home Assistant API."""
        try:
            logger.debug(
                f"[{self.name}] Rendering template from Home Assistant: '{self._context_template[:100]}...'"
            )
            rendered_template = await self._ha_client.async_get_rendered_template(
                template=self._context_template
            )
        except HomeassistantAPIError as ha_api_err:
            logger.exception(f"[{self.name}] Home Assistant API error: {ha_api_err}")
            return self._api_error_fragments()
        except Exception as e:
            logger.exception(
                f"[{self.name}] Error rendering Home Assistant template: {e}"
            )
            return self._api_error_fragments()
        return self._format_rendered_template(rendered_template)
