"""A real ``HomeAssistantClientWrapper`` over an autospecced ``homeassistant_api`` client.

The wrapper is our code and runs for real; only the third-party SDK client it
delegates to is replaced, by a ``spec_set`` autospec that rejects calls the SDK
does not have. Configure SDK responses through ``api.<method>.return_value`` /
``side_effect`` and assert on the wire-level calls the wrapper made.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import create_autospec

import homeassistant_api
from homeassistant_api.errors import BadTemplateError
from homeassistant_api.models.domains import Domain
from jinja2.sandbox import ImmutableSandboxedEnvironment

from family_assistant.plugins.home_assistant.client import (
    MAX_TEMPLATE_OUTPUT_CHARS,
    HomeAssistantClientWrapper,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
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


@dataclass(frozen=True)
class _FakeState:
    entity_id: str
    name: str
    attributes: Mapping[str, object]


class _FakeStates:
    """Home Assistant's ``states`` template global: iterable and callable."""

    def __init__(
        self,
        entities: Sequence[Mapping[str, object]],
        states: Mapping[str, tuple[str, str | None]],
    ) -> None:
        self._entities = entities
        self._states = states

    def __iter__(self) -> Iterator[_FakeState]:
        return iter([
            _FakeState(
                entity_id=str(entity["entity_id"]),
                name=str(entity["name"]),
                attributes={"device_class": entity.get("device_class")},
            )
            for entity in self._entities
        ])

    def __call__(self, entity_id: str) -> str:
        return self._states.get(entity_id, ("unknown", None))[0]


def serve_entities(
    api: NonCallableMagicMock,
    entities: Sequence[Mapping[str, object]],
    states: Mapping[str, tuple[str, str | None]] | None = None,
) -> None:
    """Render the wrapper's templates with Jinja over fixed entity data.

    The globals mirror the Home Assistant template helpers the wrapper uses,
    and output longer than Home Assistant's limit is rejected as it is there.
    An entity missing from ``states`` reports ``unknown``, as ``states()`` does.
    """
    known_states = states or {}
    by_id = {str(entity["entity_id"]): entity for entity in entities}

    def lookup(field: str) -> Callable[[str], object]:
        return lambda entity_id: by_id.get(entity_id, {}).get(field)

    def state_attr(entity_id: str, attribute: str) -> str | None:
        if attribute != "unit_of_measurement":
            return None
        return known_states.get(entity_id, ("unknown", None))[1]

    environment = ImmutableSandboxedEnvironment()
    environment.filters["tojson"] = lambda value: json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )
    environment.globals.update(
        states=_FakeStates(entities, known_states),
        state_attr=state_attr,
        area_name=lookup("area_name"),
        device_id=lookup("device_id"),
        device_name=lookup("device_name"),
    )

    async def render(template: str) -> str:
        rendered = environment.from_string(template).render()
        if len(rendered) > MAX_TEMPLATE_OUTPUT_CHARS:
            raise BadTemplateError("Your template is invalid")
        return rendered

    api.async_get_rendered_template.side_effect = render
