"""Unit tests for converting OpenAI-style tools to Gemini schema format.

Focuses on JSON Schema ``type`` handling, including the list form
(e.g. ``["string", "null"]``) used to express nullable/optional fields.
"""

import logging

import pytest
from google.genai import types

from family_assistant.json_schema import JsonSchema
from family_assistant.llm.providers.google_genai_client import (
    convert_tools_to_genai_format,
    gemini_unrepresentable_parameters,
)
from family_assistant.processing.quiet_turn import END_TURN_QUIETLY_TOOL_DEFINITION
from family_assistant.tools.catalog import TOOLS_DEFINITION
from family_assistant.tools.on_demand import ACTIVATE_TOOLS_DEFINITION
from family_assistant.tools.types import ToolDefinition, normalize_json_schema_type


def _tool_with_properties(
    properties: dict[str, JsonSchema],
    required: list[str],
) -> ToolDefinition:
    """Build a single-function tool definition around the given properties."""
    return {
        "type": "function",
        "function": {
            "name": "sample_tool",
            "description": "Sample tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _properties_of(tools: list[ToolDefinition]) -> dict[str, types.Schema]:
    converted = convert_tools_to_genai_format(tools)
    assert len(converted) == 1
    declarations = converted[0].function_declarations
    assert declarations is not None
    assert len(declarations) == 1
    parameters = declarations[0].parameters
    assert parameters is not None
    assert parameters.properties is not None
    return parameters.properties


class TestNormalizeJsonSchemaType:
    """Tests for the JSON Schema ``type`` normalization helper."""

    def test_scalar_string_type(self) -> None:
        assert normalize_json_schema_type("string") == ("string", False)

    def test_nullable_list_type_collapses_to_non_null(self) -> None:
        assert normalize_json_schema_type(["string", "null"]) == ("string", True)

    def test_null_first_ordering(self) -> None:
        assert normalize_json_schema_type(["null", "integer"]) == ("integer", True)

    def test_list_without_null_is_not_nullable(self) -> None:
        assert normalize_json_schema_type(["string", "number"]) == ("string", False)

    def test_all_null_falls_back_to_default(self) -> None:
        assert normalize_json_schema_type(["null"]) == ("string", True)

    def test_non_string_value_falls_back_to_default(self) -> None:
        assert normalize_json_schema_type(None) == ("string", False)


class TestConvertToolsToGenaiFormat:
    """Tests for converting tool definitions into Gemini schema objects."""

    def test_nullable_scalar_property_sets_nullable_flag(self) -> None:
        tools = [
            _tool_with_properties(
                {
                    "note": {
                        "type": ["string", "null"],
                        "description": "Optional note",
                    }
                },
                required=[],
            )
        ]

        note = _properties_of(tools)["note"]
        assert note.type == types.Type.STRING
        assert note.nullable is True

    def test_non_nullable_scalar_property(self) -> None:
        tools = [
            _tool_with_properties(
                {"query": {"type": "string", "description": "Query"}},
                required=["query"],
            )
        ]

        query = _properties_of(tools)["query"]
        assert query.type == types.Type.STRING
        assert not query.nullable

    def test_nullable_array_items_type(self) -> None:
        tools = [
            _tool_with_properties(
                {
                    "tags": {
                        "type": "array",
                        "description": "Tags",
                        "items": {"type": ["string", "null"]},
                    }
                },
                required=["tags"],
            )
        ]

        tags = _properties_of(tools)["tags"]
        assert tags.type == types.Type.ARRAY
        assert tags.items is not None
        assert tags.items.type == types.Type.STRING
        assert tags.items.nullable is True

    def test_boolean_property_schema_converts_as_string(self) -> None:
        tools = [_tool_with_properties({"anything": True}, required=[])]

        anything = _properties_of(tools)["anything"]
        assert anything.type == types.Type.STRING

    def test_approximated_schema_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tools = [_tool_with_properties({"anything": True}, required=[])]

        with caplog.at_level(logging.WARNING):
            convert_tools_to_genai_format(tools)

        assert "'sample_tool'" in caplog.text
        assert "anything" in caplog.text

    def test_tuple_form_items_fall_back_to_string_items(self) -> None:
        tools = [
            _tool_with_properties(
                {"pair": {"type": "array", "items": [{"type": "integer"}, True]}},
                required=[],
            )
        ]

        pair = _properties_of(tools)["pair"]
        assert pair.items is not None
        assert pair.items.type == types.Type.STRING


_LOCAL_DEFINITIONS = [
    *TOOLS_DEFINITION,
    ACTIVATE_TOOLS_DEFINITION,
    END_TURN_QUIETLY_TOOL_DEFINITION,
]


@pytest.mark.parametrize(
    "definition",
    _LOCAL_DEFINITIONS,
    ids=[definition["function"]["name"] for definition in _LOCAL_DEFINITIONS],
)
def test_local_tool_schemas_are_representable_in_gemini(
    definition: ToolDefinition,
) -> None:
    """Our own tools must not rely on the string fallback kept for MCP schemas."""
    assert gemini_unrepresentable_parameters(definition["function"]["parameters"]) == []
