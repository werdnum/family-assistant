"""Serve the household's entities from a seed file, as real Home Assistant entities.

Each integration the household's entities came from becomes one config entry
titled with that integration's domain, so ``integration_entities('wled')``
finds the same entities it does at home. Devices and areas are registered
from the seed, so the registry template functions (``area_entities``,
``device_attr``, ``device_entities``) answer from them.

Helpers, people and automations are Home Assistant's own; once started, any
whose id Home Assistant derived differently from production (an automation
named by its alias) is renamed to the production id.

Every service call is appended to the seed's ``service_log`` as one JSON
line, so a trial's end state includes what it asked the house to do -- and
what the household's own automations did in response, told apart by context. Services
that recorded turns or automations called but no served entity provides --
legacy notify targets, ``mqtt.publish`` -- are registered to accept the call
and do nothing else, as a capture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .const import (
    DATA_RECORDS,
    DATA_SEED,
    DOMAIN,
    PLATFORMS,
    SEED_FILE,
    domain_of,
    integration_of,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.typing import ConfigType

    from .const import Seed

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def _load_seed(path: str) -> Seed:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _append(path: str, line: str) -> None:
    with Path(path).open("a", encoding="utf-8") as log:
        log.write(line + "\n")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    seed = await hass.async_add_executor_job(_load_seed, hass.config.path(SEED_FILE))
    hass.data[DATA_SEED] = seed
    hass.data[DATA_RECORDS] = {}
    _log_service_calls(hass, seed["service_log"])
    areas = ar.async_get(hass)
    for area in seed["areas"]:
        areas.async_get_or_create(area["name"])
    integrations = sorted({
        integration_of(record)
        for record in seed["entities"]
        if domain_of(record) in PLATFORMS
    })
    for integration in integrations:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={"integration": integration},
            )
        )

    @callback
    def _on_started(_: Event) -> None:
        registry = er.async_get(hass)
        for rename in seed["renames"]:
            current = registry.async_get_entity_id(
                rename["domain"], rename["domain"], rename["unique_id"]
            )
            if (
                current is not None
                and current != rename["entity_id"]
                and registry.async_get(rename["entity_id"]) is None
            ):
                registry.async_update_entity(current, new_entity_id=rename["entity_id"])
        for name in seed["services"]:
            domain, service = name.split(".", 1)
            if not hass.services.has_service(domain, service):
                hass.services.async_register(domain, service, _accept)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)
    return True


async def _accept(call: ServiceCall) -> None:
    """A service the house had but this sandbox does not run: the call is logged."""


def _log_service_calls(hass: HomeAssistant, path: str) -> None:
    @callback
    def _log(event: Event) -> None:
        line = json.dumps(
            {
                "at": event.time_fired.isoformat(),
                "domain": event.data.get("domain"),
                "service": event.data.get("service"),
                "service_data": event.data.get("service_data"),
                # A call an automation or script made carries a parent
                # context; one made through the API carries the caller's user.
                "user_id": event.context.user_id,
                "parent_id": event.context.parent_id,
            },
            default=str,
        )
        hass.async_add_executor_job(_append, path, line)

    hass.bus.async_listen(EVENT_CALL_SERVICE, _log)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    seed: Seed = hass.data[DATA_SEED]
    integration = entry.data["integration"]
    records = [
        record
        for record in seed["entities"]
        if integration_of(record) == integration and domain_of(record) in PLATFORMS
    ]
    hass.data[DATA_RECORDS][entry.entry_id] = records
    platforms = sorted({PLATFORMS[domain_of(record)] for record in records})
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    records = hass.data[DATA_RECORDS].get(entry.entry_id, [])
    platforms = sorted({PLATFORMS[domain_of(record)] for record in records})
    return await hass.config_entries.async_unload_platforms(entry, platforms)
