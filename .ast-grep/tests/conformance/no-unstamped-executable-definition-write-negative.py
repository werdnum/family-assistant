"""Fixture: records obtained from the stamping chokepoint are not flagged."""

from typing import Any

from sqlalchemy import Insert, Update, insert, update

from family_assistant.security.definition_records import (
    stamp_callback_definition,
    stamp_definition,
)
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.schedule_automations import schedule_automations_table


def insert_with_a_stamped_record(name: str, content: dict[str, Any]) -> Insert:
    """The chokepoint produces the stamp, hash, and disposition together."""
    return insert(schedule_automations_table).values(
        name=name,
        definition_record=stamp_definition(
            content=content,
            taint_state=TurnTaintState.empty(),
        ).to_dict(),
    )


def update_with_a_stamped_record(
    values: dict[str, Any],
    content: dict[str, Any],
) -> None:
    """A patch-style write stamps the complete merged definition."""
    values["definition_record"] = stamp_definition(
        content=content,
        taint_state=TurnTaintState.empty(),
    ).to_dict()


def hoist_a_stamped_record(name: str, content: dict[str, Any]) -> Insert:
    """A record needed more than once is hoisted under the conventional name."""
    definition_record = stamp_definition(
        content=content,
        taint_state=TurnTaintState.empty(),
    ).to_dict()
    return insert(schedule_automations_table).values(
        name=name,
        definition_record=definition_record,
    )


def clear_a_record_deliberately() -> Update:
    """Building the pre-design legacy shape is the one legitimate literal."""
    return update(schedule_automations_table).values(definition_record=None)


def read_a_record_back_into_a_row_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Reading the stored column into a row type is not a mutation."""
    return {
        "name": row["name"],
        "definition_record": row.get("definition_record"),
    }


def build_a_row_typed_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalizing a row passes the column through as a keyword, and is not a write."""
    return dict(
        name=row["name"],
        definition_record=row.get("definition_record"),
    )


def edit_a_callback_payload_with_a_fresh_record(
    payload: dict[str, Any],
    new_context: str,
) -> None:
    """The record and the definition it describes are replaced together."""
    payload["callback_context"] = new_context
    payload["tool_call_review_definition_record"] = stamp_callback_definition(
        new_context,
        tracker=None,
    )
