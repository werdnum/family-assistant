"""Unit tests for attachment schema translation functionality."""

from typing import TYPE_CHECKING, Any, cast

import pytest

from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    translate_attachment_schemas_for_llm,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


class TestAttachmentSchemaTranslation:
    """Test suite for attachment schema translation between internal and LLM formats."""

    def test_translate_attachment_schemas_for_llm(self) -> None:
        """Test that attachment schemas are properly translated for LLM compatibility."""
        # Create tool definitions with attachment types
        original_definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "test_attachment_tool",
                    "description": "Test tool with attachment parameter",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_id": {
                                "type": "attachment",
                                "description": "image attachment to process",
                            },
                            "text_param": {
                                "type": "string",
                                "description": "regular string parameter",
                            },
                            "attachment_list": {
                                "type": "array",
                                "items": {
                                    "type": "attachment",
                                },
                                "description": "list of attachments",
                            },
                        },
                        "required": ["image_id"],
                    },
                },
            }
        ]

        # Translate schemas
        translated_definitions = translate_attachment_schemas_for_llm(
            original_definitions
        )

        # Verify structure is preserved
        assert len(translated_definitions) == 1
        assert translated_definitions[0]["type"] == "function"
        assert translated_definitions[0]["function"]["name"] == "test_attachment_tool"

        # Cast to dict for test assertions on optional TypedDict keys
        params = cast(
            "dict[str, Any]", translated_definitions[0]["function"]["parameters"]
        )
        properties = cast("dict[str, Any]", params["properties"])

        # Verify attachment parameter was translated
        image_param = properties["image_id"]
        assert image_param["type"] == "string"
        assert "UUID" in image_param["description"]
        assert "image attachment" in image_param["description"]

        # Verify regular string parameter unchanged
        text_param = properties["text_param"]
        assert text_param["type"] == "string"
        assert text_param["description"] == "regular string parameter"

        # Verify array of attachments handled correctly
        list_param = properties["attachment_list"]
        assert list_param["type"] == "array"
        assert list_param["items"]["type"] == "string"  # Should be translated
        assert "UUID" in list_param["items"]["description"]

    def test_translate_preserves_non_attachment_schemas(self) -> None:
        """Test that non-attachment schemas are preserved unchanged."""
        original_definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "regular_tool",
                    "description": "Tool without attachment parameters",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "A name"},
                            "count": {"type": "number", "description": "A count"},
                            "enabled": {"type": "boolean", "description": "A flag"},
                        },
                    },
                },
            }
        ]

        translated_definitions = translate_attachment_schemas_for_llm(
            original_definitions
        )

        # Should be identical since no attachment types present
        assert translated_definitions == original_definitions

    def test_translate_handles_missing_descriptions(self) -> None:
        """Test translation handles parameters without descriptions gracefully."""
        original_definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "attachment_param": {
                                "type": "attachment",
                                # No description provided
                            },
                        },
                    },
                },
            }
        ]

        translated_definitions = translate_attachment_schemas_for_llm(
            original_definitions
        )

        # Cast to dict for test assertions on optional TypedDict keys
        params = cast(
            "dict[str, Any]", translated_definitions[0]["function"]["parameters"]
        )
        properties = cast("dict[str, Any]", params["properties"])
        param = properties["attachment_param"]
        assert param["type"] == "string"
        assert "UUID of the attachment" in param["description"]

    @pytest.mark.asyncio
    async def test_local_tools_provider_schema_translation(self) -> None:
        """Test that LocalToolsProvider correctly translates schemas."""
        # Define a tool with attachment type
        tool_definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "process_image",
                    "description": "Process an image attachment",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_attachment_id": {
                                "type": "attachment",
                                "description": "The image to process",
                            },
                        },
                        "required": ["image_attachment_id"],
                    },
                },
            }
        ]

        # Mock implementation
        def mock_process_image(image_attachment_id: bytes) -> str:
            return f"Processed {len(image_attachment_id)} bytes"

        provider = LocalToolsProvider(
            definitions=tool_definitions,
            implementations={"process_image": mock_process_image},
        )

        # Get raw definitions (internal use) - cast for test assertions
        raw_definitions = provider.get_raw_tool_definitions()
        raw_params = cast(
            "dict[str, Any]", raw_definitions[0]["function"]["parameters"]
        )
        raw_props = cast("dict[str, Any]", raw_params["properties"])
        raw_param = raw_props["image_attachment_id"]
        assert raw_param["type"] == "attachment"

        # Get translated definitions (LLM use) - cast for test assertions
        translated_definitions = await provider.get_tool_definitions()
        trans_params = cast(
            "dict[str, Any]", translated_definitions[0]["function"]["parameters"]
        )
        trans_props = cast("dict[str, Any]", trans_params["properties"])
        translated_param = trans_props["image_attachment_id"]
        assert translated_param["type"] == "string"
        assert "UUID" in translated_param["description"]

    def test_translate_preserves_original_definitions(self) -> None:
        """Test that translation doesn't modify the original definitions."""
        original_definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test tool",
                    "parameters": {
                        "properties": {
                            "attachment_param": {
                                "type": "attachment",
                                "description": "test attachment",
                            },
                        },
                    },
                },
            }
        ]

        # Keep reference to original - cast for test assertions
        orig_params = cast(
            "dict[str, Any]", original_definitions[0]["function"]["parameters"]
        )
        orig_props = cast("dict[str, Any]", orig_params["properties"])
        original_param = orig_props["attachment_param"]
        original_type = original_param["type"]

        # Translate
        translated_definitions = translate_attachment_schemas_for_llm(
            original_definitions
        )

        # Verify original is unchanged
        assert original_param["type"] == original_type == "attachment"

        # Verify translation worked - cast for test assertions
        trans_params = cast(
            "dict[str, Any]", translated_definitions[0]["function"]["parameters"]
        )
        trans_props = cast("dict[str, Any]", trans_params["properties"])
        translated_param = trans_props["attachment_param"]
        assert translated_param["type"] == "string"
