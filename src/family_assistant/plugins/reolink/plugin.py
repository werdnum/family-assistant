"""The Reolink camera plugin."""

from __future__ import annotations

import logging
from typing import ClassVar

from family_assistant.plugins.base import Plugin
from family_assistant.plugins.reolink.backend import create_reolink_backend
from family_assistant.plugins.reolink.config import ReolinkConfig
from family_assistant.plugins.reolink.instance import ReolinkInstance
from family_assistant.plugins.reolink.tools import CAMERA_TOOLS

logger = logging.getLogger(__name__)


class ReolinkPlugin(Plugin[ReolinkConfig, ReolinkInstance]):
    """Tools for investigating Reolink camera recordings and live views."""

    id: ClassVar[str] = "reolink"
    config_model = ReolinkConfig
    tools = CAMERA_TOOLS

    def start(
        self, instance_name: str, config: ReolinkConfig
    ) -> ReolinkInstance | None:
        if not config.cameras:
            logger.warning(
                "Reolink instance %r not started: it has no cameras (REOLINK_CAMERAS "
                "for the default instance).",
                instance_name,
            )
            return None
        return ReolinkInstance(create_reolink_backend(config.cameras))


REOLINK_PLUGIN = ReolinkPlugin()
