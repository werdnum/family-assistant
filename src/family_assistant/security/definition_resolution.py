"""Firing-time resolution of stored definition records.

A definition record is written at the creation chokepoint (see
:mod:`family_assistant.security.definition_records`); this module is the other
half, where a firing asks what the stored record still entitles the definition
to. Resolution is a pure function of a record and the content it describes, so
everything database-shaped lives here: reading each artifact's current content
back, and walking the **executable closure** -- every artifact whose content a
firing is about to execute or render.

The closure is why an automation and the stored script it names are resolved
separately and then combined weakest-first, rather than hashed together. A
cross-artifact hash would rot the moment either side is edited legitimately;
resolving each against its own record means editing a shared script from a
tainted turn un-cures every automation that references it, until the script's
own gate cures it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.security.definition_records import (
    UNRESOLVED_DEFINITION,
    DefinitionResolution,
    automation_definition_content,
    callback_definition_content,
    listener_definition_content,
    resolve_definition_record,
    script_definition_content,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_assistant.storage.database import Database


@dataclass(frozen=True, slots=True)
class ScheduleAutomationRef:
    """A schedule automation, resolved against its stored row."""

    automation_id: int


@dataclass(frozen=True, slots=True)
class EventListenerRef:
    """An event listener, resolved against its stored row."""

    listener_id: int


@dataclass(frozen=True, slots=True)
class StoredScriptRef:
    """A script referenced by name, resolved against its stored body."""

    name: str


@dataclass(frozen=True, slots=True)
class CallbackPayloadRef:
    """A one-shot callback, whose definition and record ride its task payload.

    Reminders and future callbacks have no durable definition table, so there is
    no row to read the content back from: the payload *is* the definition, and
    the record travels beside the text it describes.
    """

    record: object
    definition: str | None


DefinitionRef = (
    ScheduleAutomationRef | EventListenerRef | StoredScriptRef | CallbackPayloadRef
)


async def _resolve_one(db: Database, ref: DefinitionRef) -> DefinitionResolution:
    match ref:
        case ScheduleAutomationRef(automation_id=automation_id):
            automation = await db.schedule_automations.get_by_id(automation_id)
            if automation is None:
                return UNRESOLVED_DEFINITION
            return resolve_definition_record(
                automation["definition_record"],
                automation_definition_content(
                    name=automation["name"],
                    description=automation["description"],
                    recurrence_rule=automation["recurrence_rule"],
                    action_type=automation["action_type"],
                    action_config=automation["action_config"],
                ),
            )
        case EventListenerRef(listener_id=listener_id):
            listener = await db.events.get_event_listener_by_id(listener_id)
            if listener is None:
                return UNRESOLVED_DEFINITION
            return resolve_definition_record(
                listener["definition_record"],
                listener_definition_content(
                    name=listener["name"],
                    description=listener["description"],
                    source_id=listener["source_id"],
                    match_conditions=listener["match_conditions"],
                    action_type=listener["action_type"],
                    action_config=listener["action_config"],
                    condition_script=listener["condition_script"],
                ),
            )
        case StoredScriptRef(name=name):
            script = await db.scripts.get_by_name(name)
            if script is None:
                return UNRESOLVED_DEFINITION
            return resolve_definition_record(
                script.definition_record,
                script_definition_content(
                    name=script.name,
                    description=script.description,
                    script_code=script.script_code,
                    parameters_schema=script.parameters_schema,
                ),
            )
        case CallbackPayloadRef(record=record, definition=definition):
            return resolve_definition_record(
                record, callback_definition_content(definition)
            )


async def resolve_definition_closure(
    db: Database,
    refs: Iterable[DefinitionRef],
) -> DefinitionResolution:
    """Resolve every artifact a firing executes or renders; the weakest governs.

    An empty closure is unresolved rather than trusted: a firing that can name
    no definition artifact has nothing whose authorship it could vouch for, and
    that is exactly the legacy case this design leaves fail-closed.
    """
    resolution: DefinitionResolution | None = None
    for ref in refs:
        current = await _resolve_one(db, ref)
        resolution = current if resolution is None else resolution.combine(current)
    return resolution if resolution is not None else UNRESOLVED_DEFINITION
