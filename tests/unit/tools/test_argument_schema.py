"""Unit tests for checking tool arguments against a parameter schema."""

from __future__ import annotations

import jsonschema
import pytest

from family_assistant.tools.argument_schema import argument_schema_errors

HOME_ASSISTANT_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {"type": "string"},
        "action": {"type": "string"},
        "service_data": {"type": "object", "additionalProperties": True},
    },
    "required": ["domain", "action"],
}


def test_matching_arguments_produce_no_errors() -> None:
    assert (
        argument_schema_errors(
            HOME_ASSISTANT_SCHEMA,
            {
                "domain": "vacuum",
                "action": "send_command",
                "service_data": {"entity_id": "vacuum.s8", "params": [[1, 2]]},
            },
        )
        == []
    )


def test_an_invented_argument_is_reported_alongside_the_missing_ones() -> None:
    errors = argument_schema_errors(
        HOME_ASSISTANT_SCHEMA, {"name": "vacuum_clean_under_dining_table"}
    )

    assert errors == [
        "'domain' is a required property",
        "'action' is a required property",
        "'name' is not an argument of this tool",
    ]


def test_a_wrong_type_names_the_argument() -> None:
    errors = argument_schema_errors(
        HOME_ASSISTANT_SCHEMA, {"domain": 5, "action": "turn_on"}
    )

    assert errors == ["'domain': 5 is not of type 'string'"]


def test_extra_keys_are_allowed_where_the_schema_says_so() -> None:
    assert (
        argument_schema_errors(
            HOME_ASSISTANT_SCHEMA,
            {"domain": "light", "action": "turn_on", "service_data": {"x": 1}},
        )
        == []
    )
    assert (
        argument_schema_errors(
            {"type": "object", "properties": {}, "additionalProperties": True},
            {"anything": 1},
        )
        == []
    )


def test_an_attachment_parameter_accepts_an_id() -> None:
    schema = {
        "type": "object",
        "properties": {"file": {"type": "attachment"}},
        "required": ["file"],
    }

    assert argument_schema_errors(schema, {"file": "att_123"}) == []
    assert argument_schema_errors(schema, {"file": 7}) == [
        "'file': 7 is not of type 'attachment'"
    ]


def test_an_invalid_schema_is_the_tools_fault_not_the_arguments() -> None:
    with pytest.raises(jsonschema.SchemaError):
        argument_schema_errors(
            {"type": "object", "properties": {"x": {"type": "nonsense"}}},
            {"x": 1},
        )
