"""Unit tests for ToolResult class and its fallback mechanisms."""

import json

import pytest

from family_assistant.llm.messages import tool_result_to_llm_message
from family_assistant.tools.types import ToolResult


def test_toolresult_data_only() -> None:
    """Data-only result generates text via JSON serialization."""
    result = ToolResult(data={"id": 123, "name": "test"})

    text = result.get_text()
    assert "123" in text
    assert "test" in text

    data = result.get_data()
    assert data == {"id": 123, "name": "test"}


def test_toolresult_text_only_json() -> None:
    """Text-only with valid JSON parses to data."""
    result = ToolResult(text='{"id": 456, "status": "ok"}')

    text = result.get_text()
    assert text == '{"id": 456, "status": "ok"}'

    data = result.get_data()
    assert data == {"id": 456, "status": "ok"}


def test_toolresult_text_only_non_json() -> None:
    """Text-only with non-JSON returns string as data."""
    result = ToolResult(text="Simple error message")

    text = result.get_text()
    assert text == "Simple error message"

    data = result.get_data()
    assert data == "Simple error message"  # String, not dict


def test_toolresult_both_fields() -> None:
    """Both fields: each accessible independently."""
    result = ToolResult(
        text="Created item with ID 789", data={"id": 789, "created": True}
    )

    assert result.get_text() == "Created item with ID 789"
    assert result.get_data() == {"id": 789, "created": True}


def test_toolresult_requires_at_least_one_field() -> None:
    """Cannot create ToolResult with neither text nor data."""
    with pytest.raises(ValueError, match="must have either text or data"):
        ToolResult()


def test_toolresult_data_string() -> None:
    """Data can be a string."""
    result = ToolResult(data="Just a string")

    text = result.get_text()
    assert text == "Just a string"

    data = result.get_data()
    assert data == "Just a string"


def test_toolresult_to_string_fallback() -> None:
    """to_string() uses get_text() fallback."""
    result = ToolResult(data={"value": 42})

    # to_string should use get_text which JSON-serializes data
    text = result.to_string()
    assert "42" in text
    assert "value" in text


def test_toolresult_complex_data_serialization() -> None:
    """Complex data structures serialize to readable JSON."""
    result = ToolResult(
        data={
            "id": 1,
            "items": ["a", "b", "c"],
            "nested": {"key": "value"},
            "count": 100,
        }
    )

    text = result.get_text()
    # Should be formatted JSON (indent=2)
    assert "  " in text  # Check for indentation
    parsed = json.loads(text)
    assert parsed["id"] == 1
    assert parsed["items"] == ["a", "b", "c"]


def test_toolresult_to_llm_message_uses_fallback() -> None:
    """tool_result_to_llm_message uses get_text() fallback."""
    result = ToolResult(data={"result": "success"})

    message = tool_result_to_llm_message(
        result, tool_call_id="test_123", function_name="test_tool"
    )

    assert message.role == "tool"
    assert message.tool_call_id == "test_123"
    assert message.name == "test_tool"
    # Content should use get_text() which serializes data
    assert "result" in message.content
    assert "success" in message.content


def test_toolresult_text_with_invalid_json() -> None:
    """Text that looks like JSON but isn't valid returns as string."""
    result = ToolResult(text="{not valid json}")

    data = result.get_data()
    assert isinstance(data, str)
    assert data == "{not valid json}"


def test_toolresult_empty_dict_data() -> None:
    """Empty dict is valid data."""
    result = ToolResult(data={})

    assert result.get_data() == {}
    text = result.get_text()
    assert text == "{}"


def test_toolresult_null_in_data() -> None:
    """None values in data dict are preserved."""
    result = ToolResult(data={"id": 1, "optional": None})

    data = result.get_data()
    assert data == {"id": 1, "optional": None}

    text = result.get_text()
    parsed = json.loads(text)
    assert parsed["optional"] is None
