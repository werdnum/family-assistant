"""Unit tests for checking tool arguments against a parameter schema."""

from __future__ import annotations

import jsonschema
import pytest

from family_assistant.tools.argument_schema import (
    argument_schema_errors,
    check_parameter_schema,
)

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
    for permissive in (True, {}, {"type": "integer"}):
        assert (
            argument_schema_errors(
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": permissive,
                },
                {"anything": 1},
            )
            == []
        )
    assert argument_schema_errors(
        {"type": "object", "properties": {}, "additionalProperties": False},
        {"anything": 1},
    ) == ["'anything' is not an argument of this tool"]


def test_properties_declared_through_applicators_are_known() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "allOf": [{"properties": {"x": {"type": "integer"}}, "required": ["x"]}],
        "$ref": "#/$defs/more",
        "$defs": {"more": {"properties": {"b": {"type": "boolean"}}}},
    }

    assert argument_schema_errors(schema, {"a": "s", "x": 1, "b": True}) == []
    assert argument_schema_errors(schema, {"x": 1, "y": 2, "z": 3}) == [
        "'y', 'z' are not arguments of this tool"
    ]


def test_a_root_reference_schema_still_rejects_invented_arguments() -> None:
    schema = {
        "$ref": "#/$defs/Args",
        "$defs": {"Args": {"type": "object", "properties": {"x": {"type": "integer"}}}},
    }

    assert argument_schema_errors(schema, {"x": 1}) == []
    assert argument_schema_errors(schema, {"x": 1, "invented": 2}) == [
        "'invented' is not an argument of this tool"
    ]


def test_a_schema_that_declares_nothing_takes_no_arguments() -> None:
    assert argument_schema_errors({"type": "object"}, {}) == []
    assert argument_schema_errors({"type": "object"}, {"anything": 1}) == [
        "'anything' is not an argument of this tool"
    ]


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


def test_a_resolvable_reference_passes_the_check() -> None:
    check_parameter_schema({
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/thing"}},
        "$defs": {"thing": {"type": "string"}},
    })
    check_parameter_schema({
        "type": "object",
        "properties": {"x": {"$dynamicRef": "#node"}},
        "$defs": {"node": {"$dynamicAnchor": "node", "type": "string"}},
    })


def test_reference_shaped_instance_data_is_not_a_reference() -> None:
    check_parameter_schema({
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "default": {"$ref": "literal-user-data"},
                "examples": [{"$ref": "also-literal"}],
            },
            "mode": {"const": {"$dynamicRef": "#nope"}},
            "kind": {"enum": [{"$ref": "still-data"}]},
        },
    })


def test_a_relative_reference_resolves_in_its_declaring_scope() -> None:
    check_parameter_schema({
        "$id": "https://example.test/root",
        "type": "object",
        "properties": {"x": {"$ref": "a"}},
        "$defs": {
            "a": {
                "$id": "a",
                "type": "object",
                "properties": {"y": {"$ref": "#/$defs/inner"}},
                "$defs": {"inner": {"type": "string"}},
            }
        },
    })


def test_a_parameter_schema_may_use_the_attachment_type() -> None:
    check_parameter_schema({
        "type": "object",
        "properties": {
            "file": {"type": "attachment"},
            "files": {"type": "array", "items": {"type": "attachment"}},
        },
    })


@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "object", "properties": {"x": {"type": "nonsense"}}},
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": "x"},
        {"type": "object", "properties": ["x"]},
        {"type": "object", "properties": {"x": {"$ref": "#/$defs/missing"}}},
        {"type": "object", "properties": {"x": {"$dynamicRef": "#missing"}}},
        {
            "type": "object",
            "properties": {"x": {"$ref": "https://example.invalid/schema.json"}},
        },
        {"$id": "http://[bad", "type": "object"},
    ],
)
def test_a_broken_parameter_schema_is_rejected(parameters: dict[str, object]) -> None:
    with pytest.raises(jsonschema.SchemaError):
        check_parameter_schema(parameters)
