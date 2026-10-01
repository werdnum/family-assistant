"""The Trino plugin."""

from __future__ import annotations

import logging
from typing import ClassVar

from family_assistant.plugins.base import Plugin
from family_assistant.plugins.trino.client import TrinoClient
from family_assistant.plugins.trino.config import TrinoConfig
from family_assistant.plugins.trino.instance import TrinoInstance
from family_assistant.plugins.trino.tools import TRINO_TOOLS

logger = logging.getLogger(__name__)


class TrinoPlugin(Plugin[TrinoConfig, TrinoInstance]):
    """Read-only SQL over the household data lake, graded by the tables read."""

    id: ClassVar[str] = "trino"
    config_model = TrinoConfig
    tools = TRINO_TOOLS

    def start(self, instance_name: str, config: TrinoConfig) -> TrinoInstance | None:
        if config.url is None or config.user is None:
            logger.warning(
                "Trino instance %r not started: it needs a url and a user "
                "(TRINO_URL and TRINO_USER for the default instance).",
                instance_name,
            )
            return None
        return TrinoInstance(config, TrinoClient(config))


TRINO_PLUGIN = TrinoPlugin()
