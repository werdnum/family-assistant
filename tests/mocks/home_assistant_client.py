"""A real ``HomeAssistantClientWrapper`` over an autospecced ``homeassistant_api`` client.

The wrapper is our code and runs for real; only the third-party SDK client it
delegates to is replaced, by a ``spec_set`` autospec that rejects calls the SDK
does not have. Configure SDK responses through ``api.<method>.return_value`` /
``side_effect`` and assert on the wire-level calls the wrapper made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import create_autospec

import homeassistant_api
from homeassistant_api.models.domains import Domain

from family_assistant.plugins.home_assistant.client import HomeAssistantClientWrapper

if TYPE_CHECKING:
    from unittest.mock import NonCallableMagicMock

    from homeassistant_api.utils import JSONType

HA_TEST_URL = "http://home-assistant.test:8123"
HA_TEST_TOKEN = "test-token"


@dataclass(frozen=True)
class SpeccedHomeAssistant:
    """The real wrapper and the autospecced SDK client underneath it."""

    wrapper: HomeAssistantClientWrapper
    api: NonCallableMagicMock

    def domains(self, services_json: list[dict[str, JSONType]]) -> dict[str, Domain]:
        """Build ``async_get_domains`` output from HA's ``GET /api/services`` JSON."""
        domains = [
            Domain.from_json(entry, client=cast("homeassistant_api.Client", self.api))
            for entry in services_json
        ]
        return {domain.domain_id: domain for domain in domains}


def make_home_assistant_client(
    api_url: str = HA_TEST_URL,
    *,
    token: str = HA_TEST_TOKEN,
    verify_ssl: bool = True,
) -> SpeccedHomeAssistant:
    """A wrapper whose SDK client is a ``spec_set`` autospec of ``homeassistant_api.Client``."""
    api = create_autospec(homeassistant_api.Client, instance=True, spec_set=True)
    wrapper = HomeAssistantClientWrapper(
        api_url=api_url, token=token, client=api, verify_ssl=verify_ssl
    )
    return SpeccedHomeAssistant(wrapper=wrapper, api=api)
