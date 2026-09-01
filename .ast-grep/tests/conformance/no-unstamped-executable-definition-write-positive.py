"""Fixture: definition records written without the stamping chokepoint are flagged."""

from typing import Any

from sqlalchemy import Insert, Update, insert, update

from family_assistant.storage.schedule_automations import schedule_automations_table


def insert_a_hand_assembled_record(name: str, some_hash: str) -> Insert:
    """A record whose hash was computed somewhere other than the chokepoint."""
    return insert(schedule_automations_table).values(
        name=name,
        definition_record={"version": "definition_v1", "content_hash": some_hash},
    )


def assign_a_literal_record(values: dict[str, Any], some_hash: str) -> None:
    """A write path that stamps its own record."""
    values["definition_record"] = {"content_hash": some_hash}


def carry_a_stale_record_across_a_mutation(existing: dict[str, Any]) -> Update:
    """Retaining the prior record over new content strands it against a changed hash."""
    return update(schedule_automations_table).values(
        definition_record=existing["definition_record"],
    )


def hoist_an_unstamped_record(some_hash: str) -> Update:
    """A local under the conventional name still has to come from the chokepoint."""
    definition_record = {"content_hash": some_hash}
    return update(schedule_automations_table).values(
        definition_record=definition_record,
    )


def edit_a_callback_payload_without_a_record(
    payload: dict[str, Any],
    new_context: str,
) -> None:
    """Editing a pending callback's content must obtain a fresh record."""
    payload["callback_context"] = new_context
    payload["tool_call_review_definition_record"] = {"content_hash": "stale"}
