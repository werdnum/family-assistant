"""Tests for the tools infrastructure module."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext


class TestLocalToolsProvider:
    """Test cases for LocalToolsProvider."""

    @pytest.mark.asyncio
    async def test_execute_tool_dict_result_json_formatting(self) -> None:
        """Test that dict results are properly converted to JSON strings."""

        # Define a tool that returns a dict
        async def tool_returns_dict(**kwargs: Any) -> dict:  # noqa: ANN401 # Test tool needs flexibility
            return {"status": "success", "data": {"value": 42, "message": "test"}}

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_dict",
                        "description": "Test tool that returns a dict",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_dict": tool_returns_dict},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-1",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await provider.execute_tool("tool_returns_dict", {}, context)

        # Result should be a JSON string, not Python dict string representation
        assert isinstance(result, str)
        # Should be valid JSON
        parsed = json.loads(result)
        assert parsed == {"status": "success", "data": {"value": 42, "message": "test"}}
        # Should NOT contain Python-style single quotes
        assert "'" not in result
        # Should contain proper JSON double quotes
        assert '"status"' in result
        assert '"success"' in result

    @pytest.mark.asyncio
    async def test_execute_tool_list_result_json_formatting(self) -> None:
        """Test that list results are properly converted to JSON strings."""

        # Define a tool that returns a list
        async def tool_returns_list(**kwargs: Any) -> list:  # noqa: ANN401 # Test tool needs flexibility
            return [
                {"id": 1, "name": "first"},
                {"id": 2, "name": "second"},
                {"id": 3, "name": "third"},
            ]

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_list",
                        "description": "Test tool that returns a list",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_list": tool_returns_list},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-2",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await provider.execute_tool("tool_returns_list", {}, context)

        # Result should be a JSON string
        assert isinstance(result, str)
        # Should be valid JSON
        parsed = json.loads(result)
        assert len(parsed) == 3
        assert parsed[0] == {"id": 1, "name": "first"}
        # Should NOT contain Python-style single quotes
        assert "'" not in result
        # Should be properly formatted JSON
        assert '"id"' in result
        assert '"name"' in result

    @pytest.mark.asyncio
    async def test_execute_tool_complex_nested_result_json_formatting(self) -> None:
        """Test that complex nested structures are properly converted to JSON."""

        # Define a tool that returns a complex nested structure
        async def tool_returns_complex(**kwargs: Any) -> dict:  # noqa: ANN401 # Test tool needs flexibility
            return {
                "metadata": {"version": "1.0", "timestamp": "2025-01-01T00:00:00Z"},
                "items": [
                    {"type": "A", "values": [1, 2, 3], "active": True},
                    {"type": "B", "values": [4, 5, 6], "active": False},
                ],
                "summary": {"total": 6, "types": ["A", "B"]},
                "special_chars": {
                    "unicode": "Hello 世界",
                    "quotes": 'test "quoted" value',
                },
            }

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_complex",
                        "description": "Test tool that returns complex nested data",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_complex": tool_returns_complex},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-3",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await provider.execute_tool("tool_returns_complex", {}, context)

        # Result should be valid JSON
        assert isinstance(result, str)
        parsed = json.loads(result)

        # Check structure is preserved
        assert "metadata" in parsed
        assert parsed["metadata"]["version"] == "1.0"
        assert len(parsed["items"]) == 2
        assert parsed["summary"]["total"] == 6

        # Check special characters are handled correctly
        assert parsed["special_chars"]["unicode"] == "Hello 世界"
        assert parsed["special_chars"]["quotes"] == 'test "quoted" value'

        # Ensure it's proper JSON formatting
        assert "'" not in result or (
            '"' in result and result.count('"') > result.count("'")
        )
        assert result.strip().startswith("{")
        assert result.strip().endswith("}")

    @pytest.mark.asyncio
    async def test_execute_tool_none_result_handling(self) -> None:
        """Test that None results are handled gracefully."""

        # Define a tool that returns None
        async def tool_returns_none(**kwargs: Any) -> None:  # noqa: ANN401 # Test tool needs flexibility
            return None

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_none",
                        "description": "Test tool that returns None",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_none": tool_returns_none},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-4",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await provider.execute_tool("tool_returns_none", {}, context)

        # Should return a descriptive message
        assert isinstance(result, str)
        assert "None" in result
        assert "successfully" in result

    @pytest.mark.asyncio
    async def test_execute_tool_string_result_unchanged(self) -> None:
        """Test that string results are returned unchanged."""

        # Define a tool that returns a string
        async def tool_returns_string(**kwargs: Any) -> str:  # noqa: ANN401 # Test tool needs flexibility
            return "This is a plain string result"

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_string",
                        "description": "Test tool that returns a string",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_string": tool_returns_string},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-5",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await provider.execute_tool("tool_returns_string", {}, context)

        # Should return the string unchanged
        assert result == "This is a plain string result"

    @pytest.mark.asyncio
    async def test_execute_tool_number_result_stringified(self) -> None:
        """Test that numeric results are converted to strings."""

        # Define a tool that returns a number
        async def tool_returns_number(**kwargs: Any) -> int:  # noqa: ANN401 # Test tool needs flexibility
            return 42

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_returns_number",
                        "description": "Test tool that returns a number",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            implementations={"tool_returns_number": tool_returns_number},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-6",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await provider.execute_tool("tool_returns_number", {}, context)

        # Should be converted to string
        assert isinstance(result, str)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_execute_tool_invalid_attachment_fails_gracefully(self) -> None:
        """Test that tools with invalid attachment IDs fail with proper error messages."""

        # Define a tool that expects an attachment parameter
        async def tool_with_attachment(
            exec_context: ToolExecutionContext,
            image_attachment_id: Any,  # noqa: ANN401
        ) -> str:
            # This should never be reached when attachment is invalid
            return f"Processed attachment: {image_attachment_id}"

        provider = LocalToolsProvider(
            definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_with_attachment",
                        "description": "Test tool that requires an attachment",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "image_attachment_id": {
                                    "type": "attachment",
                                    "description": "An attachment ID",
                                }
                            },
                            "required": ["image_attachment_id"],
                        },
                    },
                }
            ],
            implementations={"tool_with_attachment": tool_with_attachment},
        )

        mock_db_context = MagicMock(spec=DatabaseContext)
        context = ToolExecutionContext(
            conversation_id="test-conv-attachment",
            user_name="test-user",
            interface_type="test",
            timezone_str="UTC",
            turn_id=None,
            db_context=mock_db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,  # No attachment registry
        )

        # Test with a valid UUID format but non-existent attachment
        result = await provider.execute_tool(
            "tool_with_attachment",
            {"image_attachment_id": "5d8f4d9c-8a8d-4f9e-8b3a-9b7e3d6a1b1a"},
            context,
        )

        # Should return an error message, not crash
        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert "not found or access denied" in result
        assert "image_attachment_id" in result
