"""Functional tests for data manipulation tools."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.data_manipulation import jq_query_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


async def _store_test_attachment(
    registry: AttachmentRegistry,
    db_context: DatabaseContext,
    content: bytes,
    filename: str,
    mime_type: str,
    conversation_id: str,
) -> str:
    """Helper to store an attachment using registry's proper API."""
    attachment_id = str(uuid.uuid4())

    # Use registry's internal method to get proper file path
    file_path = registry._get_file_path(attachment_id, filename)

    # Write file
    file_path.write_bytes(content)

    # Register in database
    await registry.register_attachment(
        db_context=db_context,
        attachment_id=attachment_id,
        source_type="tool",
        source_id="test",
        mime_type=mime_type,
        description=f"Test {mime_type} data",
        size=len(content),
        storage_path=str(file_path),
        conversation_id=conversation_id,
    )

    return attachment_id


@pytest.fixture
async def attachment_registry_with_json(
    db_engine: AsyncEngine, tmp_path: Path
) -> tuple[AttachmentRegistry, str, str]:
    """
    Create an attachment registry with a JSON attachment.

    Returns:
        Tuple of (registry, attachment_id, conversation_id)
    """
    storage_path = tmp_path / "attachments"
    storage_path.mkdir(parents=True, exist_ok=True)

    registry = AttachmentRegistry(
        storage_path=str(storage_path), db_engine=db_engine, config=None
    )

    # Create test JSON data
    test_data = {
        "items": [
            {"id": 1, "name": "Alice", "age": 30, "city": "New York"},
            {"id": 2, "name": "Bob", "age": 25, "city": "Los Angeles"},
            {"id": 3, "name": "Charlie", "age": 35, "city": "Chicago"},
        ],
        "metadata": {"total": 3, "source": "test"},
    }

    json_bytes = json.dumps(test_data).encode("utf-8")
    conversation_id = "test_conversation"

    # Store attachment using helper (which uses registry's proper API)
    async with DatabaseContext(db_engine) as db_context:
        attachment_id = await _store_test_attachment(
            registry=registry,
            db_context=db_context,
            content=json_bytes,
            filename="test_data.json",
            mime_type="application/json",
            conversation_id=conversation_id,
        )

    return registry, attachment_id, conversation_id


class TestJqQueryTool:
    """Test the jq_query tool functionality."""

    async def test_jq_query_basic(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test basic jq query on JSON attachment."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query: get all items
            result = await jq_query_tool(
                exec_context, attachment_id=attachment_id, jq_program=".items"
            )

            # Check structured data (returned directly for scripts)
            assert result.data is not None
            assert isinstance(result.data, list)
            assert len(result.data) == 3
            assert result.data[0]["name"] == "Alice"  # type: ignore[index,call-overload]
            assert result.data[1]["name"] == "Bob"  # type: ignore[index,call-overload]
            assert result.data[2]["name"] == "Charlie"  # type: ignore[index,call-overload]

            # Check text representation (auto-generated for LLM)
            text = result.get_text()
            assert "Alice" in text
            assert "Bob" in text
            assert "Charlie" in text

    async def test_jq_query_count(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test jq query to count items."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query: count items
            result = await jq_query_tool(
                exec_context, attachment_id=attachment_id, jq_program=".items | length"
            )

            # Check structured data (single value unwrapped)
            assert result.data == 3

            # Check text representation
            text = result.get_text()
            assert "3" in text

    async def test_jq_query_first_item(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test jq query to get first item."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query: get first item
            result = await jq_query_tool(
                exec_context, attachment_id=attachment_id, jq_program=".items[0]"
            )

            # Check structured data (single item unwrapped)
            assert result.data is not None
            assert isinstance(result.data, dict)
            assert result.data["name"] == "Alice"  # type: ignore[call-overload]
            assert result.data["city"] == "New York"  # type: ignore[call-overload]

            # Check text representation
            text = result.get_text()
            assert "Alice" in text
            assert "New York" in text
            assert "Bob" not in text

    async def test_jq_query_map_field(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test jq query to map/extract a specific field."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query: extract all names
            result = await jq_query_tool(
                exec_context,
                attachment_id=attachment_id,
                jq_program=".items | map(.name)",
            )

            # Check structured data (list of names)
            assert result.data is not None
            assert isinstance(result.data, list)
            assert result.data == ["Alice", "Bob", "Charlie"]

            # Check text representation
            text = result.get_text()
            assert "Alice" in text
            assert "Bob" in text
            assert "Charlie" in text

    async def test_jq_query_date_range(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test jq query to get date range (first and last item)."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query: get IDs of first and last item
            result = await jq_query_tool(
                exec_context,
                attachment_id=attachment_id,
                jq_program="[.items[0].id, .items[-1].id]",
            )

            # Check structured data (list of IDs)
            assert result.data is not None
            assert isinstance(result.data, list)
            assert result.data == [1, 3]

            # Check text representation
            text = result.get_text()
            assert "1" in text
            assert "3" in text

    async def test_jq_query_invalid_program(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test jq query with invalid jq syntax."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query: invalid jq syntax
            result = await jq_query_tool(
                exec_context,
                attachment_id=attachment_id,
                jq_program="invalid jq syntax [",
            )

            assert result.text is not None
            assert "Error" in result.text
            assert "Invalid jq query" in result.text

    async def test_jq_query_attachment_not_found(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """Test jq query with non-existent attachment."""
        storage_path = tmp_path / "attachments"
        storage_path.mkdir(parents=True, exist_ok=True)

        registry = AttachmentRegistry(
            storage_path=str(storage_path), db_engine=db_engine, config=None
        )

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            # Query with non-existent attachment ID
            result = await jq_query_tool(
                exec_context,
                attachment_id="nonexistent-uuid",
                jq_program=".items",
            )

            assert result.text is not None
            assert "Error" in result.text
            assert "not found" in result.text

    async def test_jq_query_cross_conversation_access_denied(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test that jq query prevents cross-conversation attachment access."""
        registry, attachment_id, _conversation_id = attachment_registry_with_json

        async with DatabaseContext(db_engine) as db_context:
            # Try to access from a different conversation
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="different_conversation",
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            result = await jq_query_tool(
                exec_context, attachment_id=attachment_id, jq_program=".items"
            )

            assert result.text is not None
            assert "Error" in result.text
            assert "Access denied" in result.text or "not accessible" in result.text

    async def test_jq_query_non_json_attachment(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """Test jq query on non-JSON attachment."""
        storage_path = tmp_path / "attachments"
        storage_path.mkdir(parents=True, exist_ok=True)

        registry = AttachmentRegistry(
            storage_path=str(storage_path), db_engine=db_engine, config=None
        )

        conversation_id = "test_conversation"
        text_content = b"This is not JSON"

        async with DatabaseContext(db_engine) as db_context:
            # Store attachment using helper
            attachment_id = await _store_test_attachment(
                registry=registry,
                db_context=db_context,
                content=text_content,
                filename="test.txt",
                mime_type="text/plain",
                conversation_id=conversation_id,
            )

            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id=conversation_id,
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            result = await jq_query_tool(
                exec_context, attachment_id=attachment_id, jq_program=".items"
            )

            assert result.text is not None
            assert "Error" in result.text
            assert "not valid JSON" in result.text

    async def test_jq_query_no_attachment_registry(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test jq query when attachment registry is not available."""
        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="TestUser",
                turn_id="test-turn",
                db_context=db_context,
                attachment_registry=None,  # No registry,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources={},
            )

            result = await jq_query_tool(
                exec_context,
                attachment_id="some-id",
                jq_program=".items",
            )

            assert result.text is not None
            assert "Error" in result.text
            assert "Attachment registry not available" in result.text
