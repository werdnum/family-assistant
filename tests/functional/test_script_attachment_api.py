"""Tests for Starlark script attachment API functionality."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from family_assistant.scripting.apis.attachments import (
    AttachmentAPI,
    create_attachment_api,
)
from family_assistant.scripting.engine import StarlarkConfig, StarlarkEngine
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.attachments import AttachmentService
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


@pytest.fixture
async def attachment_service(tmp_path: Path) -> AttachmentService:
    """Create a real AttachmentService for testing."""

    # Create a temporary directory for test attachments
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    return AttachmentService(storage_path=str(test_storage))


@pytest.fixture
async def attachment_registry(
    attachment_service: AttachmentService,
) -> AttachmentRegistry:
    """Create a real AttachmentRegistry for testing."""
    return AttachmentRegistry(attachment_service)


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
            main_loop=None,
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
            main_loop=None,
            db_engine=db_engine,
        )

        fake_id = str(uuid.uuid4())
        result = await api._get_async(fake_id)

        assert result is None

    async def test_get_attachment_wrong_conversation(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test getting attachment from different conversation is blocked."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="different_conversation",  # Different conversation
            main_loop=None,
            db_engine=db_engine,
        )

        result = await api._get_async(sample_attachment)

        # Should return None due to conversation mismatch
        assert result is None

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
            main_loop=None,
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
            main_loop=None,
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
            main_loop=None,
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
            main_loop=None,
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
            main_loop=None,
            db_engine=db_engine,
        )

        fake_id = str(uuid.uuid4())
        result = await api._send_async(fake_id)

        assert "not found" in result.lower()
        assert fake_id in result

    async def test_send_attachment_wrong_conversation(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test sending attachment from different conversation is blocked."""
        api = AttachmentAPI(
            attachment_registry=attachment_registry,
            conversation_id="different_conversation",
            main_loop=None,
            db_engine=db_engine,
        )

        result = await api._send_async(sample_attachment)

        assert "not accessible" in result.lower()


class TestCreateAttachmentAPI:
    """Test the create_attachment_api factory function."""

    async def test_create_api_with_attachment_service(
        self,
        db_engine: AsyncEngine,
        attachment_service: AttachmentService,
    ) -> None:
        """Test creating AttachmentAPI from execution context."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                attachment_service=attachment_service,
            )

            api = create_attachment_api(execution_context)

            assert isinstance(api, AttachmentAPI)
            assert api.conversation_id == "test_conversation"

    async def test_create_api_without_attachment_service(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test creating AttachmentAPI fails without attachment service."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                attachment_service=None,  # No attachment service
            )

            with pytest.raises(RuntimeError, match="AttachmentService not available"):
                create_attachment_api(execution_context)


class TestStarlarkIntegration:
    """Test attachment API integration with Starlark scripts."""

    async def test_script_without_attachment_service(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test that scripts work without attachment service (functions not available)."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                attachment_service=None,  # No attachment service
            )

            config = StarlarkConfig(enable_print=True)
            engine = StarlarkEngine(config=config)

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
        attachment_service: AttachmentService,
        sample_attachment: str,
    ) -> None:
        """Test that attachment functions are available in Starlark scripts."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                attachment_service=attachment_service,
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

            config = StarlarkConfig(enable_print=True)
            engine = StarlarkEngine(tools_provider=tools_provider, config=config)

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
        attachment_service: AttachmentService,
    ) -> None:
        """Test that attachment function errors are handled gracefully."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                attachment_service=attachment_service,
            )

            config = StarlarkConfig(enable_print=True)
            engine = StarlarkEngine(config=config)

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
        attachment_service: AttachmentService,
    ) -> None:
        """Test that attachment_list function is not available in scripts for security."""
        async with DatabaseContext(engine=db_engine) as db_context:
            execution_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                attachment_service=attachment_service,
            )

            config = StarlarkConfig(enable_print=True)
            engine = StarlarkEngine(config=config)

            # Script that tries to call attachment_list (should fail)
            script = """
# This should raise a NameError since attachment_list is not available
attachment_list()
"""

            # Expect ScriptExecutionError due to NameError
            with pytest.raises(
                ScriptExecutionError, match="Variable.*attachment_list.*not found"
            ):
                await engine.evaluate_async(
                    script=script,
                    execution_context=execution_context,
                )
