"""Tests for script attachment API functionality."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import pytest

from family_assistant.scripting.apis.attachments import (
    AttachmentAPI,
    create_attachment_api,
)
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    ATTACHMENT_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools import AVAILABLE_FUNCTIONS as local_tool_implementations
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


# AttachmentService fixture removed - using AttachmentRegistry directly


@pytest.fixture
async def attachment_registry(
    tmp_path: Path,
    db_engine: AsyncEngine,
) -> AttachmentRegistry:
    """Create a real AttachmentRegistry for testing."""
    # Create a temporary directory for test attachments
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    return AttachmentRegistry(
        storage_path=str(test_storage), db_engine=db_engine, config=None
    )


@pytest.fixture
async def sample_attachment(
    db_engine: AsyncEngine, attachment_registry: AttachmentRegistry
) -> str:
    """Create a real attachment in the database and return its ID."""
    async with DatabaseContext(engine=db_engine) as db_context:
        # Register a user attachment
        attachment_record = await attachment_registry.register_user_attachment(
            db_context=db_context,
            content=b"Test attachment content",
            mime_type="text/plain",
            filename="test.txt",
            conversation_id="test_conversation",
            user_id="test_user",
            description="Test attachment",
        )
        return attachment_record.attachment_id


class TestAttachmentAPI:
    """Test the AttachmentAPI class with real database operations."""

    async def test_get_attachment_success(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test getting attachment metadata successfully."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        result = await api._get_async(sample_attachment)

        assert result is not None
        assert result["attachment_id"] == sample_attachment
        assert result["source_type"] == "user"
        assert result["mime_type"] == "text/plain"
        assert result["description"] == "Test attachment"
        assert result["conversation_id"] == "test_conversation"

    async def test_get_attachment_not_found(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test getting non-existent attachment returns None."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        fake_id = str(uuid.uuid4())
        result = await api._get_async(fake_id)

        assert result is None

    async def test_get_attachment_cross_conversation_success(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test getting attachment from different conversation is allowed."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="different_conversation",  # Different conversation
            db_engine=db_engine,
        )

        result = await api._get_async(sample_attachment)

        # Should succeed regardless of conversation
        assert result is not None
        assert result["attachment_id"] == sample_attachment

    async def test_list_attachments_success(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test listing attachments successfully."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        result = await api._list_async()

        assert len(result) >= 1
        # Find our attachment in the results
        our_attachment = next(
            (att for att in result if att["attachment_id"] == sample_attachment), None
        )
        assert our_attachment is not None
        assert our_attachment["source_type"] == "user"
        assert our_attachment["mime_type"] == "text/plain"

    async def test_list_attachments_filter_by_source(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test listing attachments filtered by source type."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        # Filter for user attachments
        result = await api._list_async(source_type="user", limit=10)

        assert len(result) >= 1
        # All results should be user attachments
        for att in result:
            assert att["source_type"] == "user"

    async def test_list_attachments_empty_conversation(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test listing attachments in empty conversation."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="empty_conversation",
            db_engine=db_engine,
        )

        result = await api._list_async()
        assert result == []

    async def test_send_attachment_success(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test sending attachment successfully."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        result = await api._send_async(sample_attachment, "Here's your file")

        assert "sent attachment" in result.lower()
        assert sample_attachment in result

    async def test_send_attachment_not_found(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test sending non-existent attachment."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        fake_id = str(uuid.uuid4())
        result = await api._send_async(fake_id)

        assert "not found" in result.lower()
        assert fake_id in result

    async def test_send_attachment_cross_conversation_success(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test sending attachment from different conversation is allowed."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="different_conversation",
            db_engine=db_engine,
        )

        result = await api._send_async(sample_attachment)

        assert "sent attachment" in result.lower()

    async def test_create_attachment_with_string_content(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating attachment with string content."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        content = "Hello, world!"
        metadata = await api._create_async(
            content=content,
            filename="test.txt",
            description="Test text file",
            mime_type="text/plain",
        )

        # Verify attachment was created
        assert metadata is not None
        assert metadata.attachment_id is not None
        assert len(metadata.attachment_id) == 36  # UUID format

        # Verify we can retrieve it
        async with DatabaseContext(engine=db_engine) as db_context:
            retrieved_metadata = await attachment_registry.get_attachment(
                db_context, metadata.attachment_id
            )
            assert retrieved_metadata is not None
            assert retrieved_metadata.source_type == "script"
            assert retrieved_metadata.mime_type == "text/plain"
            assert retrieved_metadata.description == "Test text file"
            assert retrieved_metadata.conversation_id == "test_conversation"

            # Verify content
            retrieved_content = await attachment_registry.get_attachment_content(
                db_context, metadata.attachment_id
            )
            assert retrieved_content == content.encode("utf-8")

    async def test_create_attachment_with_bytes_content(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating attachment with bytes content."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        content = b"Binary data here"
        metadata = await api._create_async(
            content=content,
            filename="binary.txt",
            description="Binary file",
            mime_type="text/plain",
        )

        # Verify attachment was created
        assert metadata is not None
        assert metadata.attachment_id is not None

        # Verify content
        async with DatabaseContext(engine=db_engine) as db_context:
            retrieved_content = await attachment_registry.get_attachment_content(
                db_context, metadata.attachment_id
            )
            assert retrieved_content == content

    async def test_create_attachment_with_json_content(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating attachment with JSON content (stored as text/plain)."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="test_conversation",
            db_engine=db_engine,
        )

        json_data = {"key": "value", "number": 42, "list": [1, 2, 3]}
        content = json.dumps(json_data)

        metadata = await api._create_async(
            content=content,
            filename="data.json",
            description="JSON data",
            mime_type="text/plain",  # Use text/plain since application/json not in allowed list
        )

        # Verify attachment was created and content is correct
        async with DatabaseContext(engine=db_engine) as db_context:
            retrieved_content = await attachment_registry.get_attachment_content(
                db_context, metadata.attachment_id
            )
            assert retrieved_content is not None
            retrieved_data = json.loads(retrieved_content.decode("utf-8"))
            assert retrieved_data == json_data

    async def test_create_attachment_cross_conversation_accessible(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that created attachments are accessible from other conversations."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="conversation_a",
            db_engine=db_engine,
        )

        metadata = await api._create_async(
            content="Test content",
            filename="test.txt",
            description="Test file",
            mime_type="text/plain",
        )

        # Verify it's accessible from the same conversation
        same_api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="conversation_a",
            db_engine=db_engine,
        )
        result = await same_api._get_async(metadata.attachment_id)
        assert result is not None

        # Verify it's also accessible from a different conversation
        different_api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="conversation_b",
            db_engine=db_engine,
        )
        result = await different_api._get_async(metadata.attachment_id)
        assert result is not None
        assert result["attachment_id"] == metadata.attachment_id


class TestCreateAttachmentAPI:
    """Test the create_attachment_api factory function."""

    async def test_create_api_with_attachment_registry(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating AttachmentAPI from execution context."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            api = create_attachment_api(execution_context)

            assert isinstance(api, AttachmentAPI)
            assert api.conversation_id == "test_conversation"

    async def test_create_api_without_attachment_registry(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test creating AttachmentAPI fails without attachment registry."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,  # No attachment registry
                camera_backend=None,
            )

            with pytest.raises(
                RuntimeError,
                match="AttachmentRegistry not available in execution context",
            ):
                create_attachment_api(execution_context)


class TestScriptIntegration:
    """Test attachment API integration with scripts."""

    async def test_script_without_attachment_registry(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test that scripts work without attachment registry (functions not available)."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,  # No attachment registry
                camera_backend=None,
            )

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(config=config)

            # Simple script that doesn't use attachment functions
            script = """
print("Hello world")
"success"
"""

            result = await engine.evaluate_async(
                script=script,
                execution_context=execution_context,
            )

            assert result == "success"

    async def test_script_with_attachment_functions(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test that attachment functions are available in scripts."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            # Create tools provider with attachment tools
            local_provider = LocalToolsProvider(
                definitions=ATTACHMENT_TOOLS_DEFINITION,
                implementations={
                    "attach_to_response": local_tool_implementations[
                        "attach_to_response"
                    ],
                },
            )
            tools_provider = CompositeToolsProvider(providers=[local_provider])
            await tools_provider.get_tool_definitions()

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(tools_provider=tools_provider, config=config)

            # Test script that uses attachment functions
            # Note: attachment_list is not available for security, so we test with known attachment ID
            script = f"""
# Test attachment_get with known ID from test setup
attachment_id = "{sample_attachment}"
metadata = attachment_get(attachment_id)

if metadata:
    print("Found attachment:", metadata.get("description", "No description"))
    # Test using attachment with LLM tools (attachment_send was removed)
    # We can test that attach_to_response tool is available via tools_execute
    attach_result = tools_execute("attach_to_response", attachment_ids=[attachment_id])
    print("Attach result:", attach_result)
    result = True
else:
    print("No attachment found")
    result = False

result
"""

            result = await engine.evaluate_async(
                script=script,
                execution_context=execution_context,
            )

            # Should return True since we found attachment
            assert result is True

    async def test_script_attachment_error_handling(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that attachment function errors are handled gracefully."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(config=config)

            # Script that tries to get non-existent attachment
            script = """
fake_id = "00000000-0000-0000-0000-000000000000"
result = attachment_get(fake_id)
result == None
"""

            result = await engine.evaluate_async(
                script=script,
                execution_context=execution_context,
            )

            # Should return True (result is None)
            assert result is True

    async def test_attachment_list_not_available(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that attachment_list function is not available in scripts for security."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(config=config)

            # Script that tries to call attachment_list (should fail)
            script = """
# This should raise a NameError since attachment_list is not available
attachment_list()
"""

            # Expect ScriptExecutionError due to NameError
            with pytest.raises(
                ScriptExecutionError, match="attachment_list.*not defined"
            ):
                await engine.evaluate_async(
                    script=script,
                    execution_context=execution_context,
                )

    async def test_script_create_text_attachment(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating a text attachment from within a script."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(config=config)

            # Script that creates a text attachment
            script = """
content = "Hello from script!"
attachment_id = attachment_create(
    content=content,
    filename="script-output.txt",
    description="Script-generated text file",
    mime_type="text/plain"
)
print("Created attachment:", attachment_id)
attachment_id
"""

            result = await engine.evaluate_async(
                script=script,
                execution_context=execution_context,
            )

            # Verify result is a dict with metadata
            assert isinstance(result, dict)
            assert "id" in result
            assert "filename" in result
            assert "mime_type" in result
            assert result["filename"] == "script-output.txt"
            assert result["mime_type"] == "text/plain"

            # Extract the attachment ID
            attachment_id = result["id"]
            assert len(attachment_id) == 36  # UUID format

        # Verify the attachment exists and has correct metadata
        async with DatabaseContext(engine=db_engine) as verify_context:
            metadata = await attachment_registry.get_attachment(
                verify_context, attachment_id
            )
            assert metadata is not None
            assert metadata.source_type == "script"
            assert metadata.mime_type == "text/plain"
            assert metadata.description == "Script-generated text file"
            assert metadata.conversation_id == "test_conversation"

            # Verify content
            content = await attachment_registry.get_attachment_content(
                verify_context, attachment_id
            )
            assert content == b"Hello from script!"

    async def test_script_create_json_attachment(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating a JSON attachment from within a script."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(config=config)

            # Script that creates a JSON attachment (stored as text/plain)
            script = """
# Create a data structure
data = {
    "name": "Test",
    "count": 42,
    "items": ["a", "b", "c"]
}

# Encode to JSON
json_content = json_encode(data)

# Create attachment (using text/plain since application/json not in allowed list)
attachment_id = attachment_create(
    content=json_content,
    filename="data.json",
    description="JSON data from script",
    mime_type="text/plain"
)

# Return the attachment_id for verification
attachment_id
"""

            result = await engine.evaluate_async(
                script=script,
                execution_context=execution_context,
            )

            # Verify result is a dict with metadata
            assert isinstance(result, dict)
            assert "id" in result
            assert "filename" in result
            assert result["filename"] == "data.json"

            # Extract the attachment ID
            attachment_id = result["id"]
            assert len(attachment_id) == 36  # UUID format

        # Verify the attachment content is valid JSON
        async with DatabaseContext(engine=db_engine) as verify_context:
            content = await attachment_registry.get_attachment_content(
                verify_context, attachment_id
            )
            assert content is not None
            data = json.loads(content.decode("utf-8"))
            assert data["name"] == "Test"
            assert data["count"] == 42
            assert data["items"] == ["a", "b", "c"]

    async def test_script_create_and_retrieve_attachment(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating an attachment and then retrieving it in the same script."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                camera_backend=None,
            )

            config = ScriptConfig(enable_print=True)
            engine = MontyEngine(config=config)

            # Script that creates an attachment and retrieves it
            script = """
# Create attachment (returns dict with metadata)
attachment_dict = attachment_create(
    content="Test content",
    filename="test.txt",
    description="Test file",
    mime_type="text/plain"
)

# Extract ID from the dict
attachment_id = attachment_dict["id"]

# Retrieve metadata using the ID
metadata = attachment_get(attachment_id)

# Return both ID and description from the original dict
{
    "id": attachment_id,
    "description": attachment_dict.get("description")
}
"""

            result = await engine.evaluate_async(
                script=script,
                execution_context=execution_context,
            )

            # Verify result
            assert isinstance(result, dict)
            assert "id" in result
            assert result["description"] == "Test file"
