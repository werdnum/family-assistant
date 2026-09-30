"""Tests for script attachment API functionality."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.scripting.apis.attachments import create_attachment_api
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
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
    db_context = Database(engine=db_engine)
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


def _script_context(
    db_engine: AsyncEngine,
    attachment_registry: AttachmentRegistry,
    *,
    conversation_id: str = "test_conversation",
    taint_tracker: InMemoryTurnTaintTracker | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id=conversation_id,
        user_name="test_user",
        turn_id="test_turn",
        db_context=Database(engine=db_engine),
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        taint_tracker=taint_tracker,
    )


def _engine() -> MontyEngine:
    return MontyEngine(config=ScriptConfig(), default_timezone=ZoneInfo("UTC"))


class TestCreateAttachmentAPI:
    """Test the create_attachment_api factory function."""

    async def test_create_api_without_attachment_registry(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test creating AttachmentAPI fails without attachment registry."""
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=None,  # No attachment registry
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
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
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=None,  # No attachment registry
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

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

    async def test_script_get_returns_attachment_metadata(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        result = await _engine().evaluate_async(
            script=f'attachment_get("{sample_attachment}")',
            execution_context=_script_context(db_engine, attachment_registry),
        )

        assert result["attachment_id"] == sample_attachment
        assert result["source_type"] == "user"
        assert result["mime_type"] == "text/plain"
        assert result["description"] == "Test attachment"
        assert result["conversation_id"] == "test_conversation"

    async def test_script_get_reaches_an_attachment_from_another_conversation(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        result = await _engine().evaluate_async(
            script=f'attachment_get("{sample_attachment}")',
            execution_context=_script_context(
                db_engine,
                attachment_registry,
                conversation_id="different_conversation",
            ),
        )

        assert result["attachment_id"] == sample_attachment
        assert result["conversation_id"] == "test_conversation"

    async def test_script_with_attachment_functions(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """A script can look up an attachment and hand its ID to a tool."""
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        # Create tools provider with attachment tools
        local_provider = LocalToolsProvider(
            definitions=ATTACHMENT_TOOLS_DEFINITION,
            implementations={
                "attach_to_response": local_tool_implementations["attach_to_response"],
            },
        )
        tools_provider = CompositeToolsProvider(providers=[local_provider])
        await tools_provider.get_tool_definitions()

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            tools_provider=tools_provider,
            config=config,
            default_timezone=ZoneInfo("Australia/Sydney"),
        )

        script = f"""
metadata = attachment_get("{sample_attachment}")
attach_result = tools_execute("attach_to_response", attachment_ids=[metadata["attachment_id"]])
{{"metadata": metadata, "attach": attach_result}}
"""

        result = await engine.evaluate_async(
            script=script,
            execution_context=execution_context,
        )

        assert result["metadata"]["attachment_id"] == sample_attachment
        assert result["metadata"]["description"] == "Test attachment"
        attach = json.loads(result["attach"])
        assert attach["status"] == "attachments_queued"
        assert attach["attachment_ids"] == [sample_attachment]

    async def test_script_attachment_error_handling(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that attachment function errors are handled gracefully."""
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

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
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

        # Script that tries to call attachment_list (should fail)
        script = """
# This should raise a NameError since attachment_list is not available
attachment_list()
"""

        # Expect ScriptExecutionError due to NameError
        with pytest.raises(ScriptExecutionError, match="attachment_list.*not defined"):
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
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

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
        verify_context = Database(engine=db_engine)
        metadata = await attachment_registry.get_attachment(
            verify_context, attachment_id, acting_user_id=None
        )
        assert metadata is not None
        assert metadata.source_type == "script"
        assert metadata.mime_type == "text/plain"
        assert metadata.description == "Script-generated text file"
        assert metadata.conversation_id == "test_conversation"

        # Verify content
        content = await attachment_registry.get_attachment_content(
            verify_context, attachment_id, acting_user_id=None
        )
        assert content == b"Hello from script!"

    async def test_script_create_attachment_from_bytes(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Bytes content is stored as given, without a text round trip."""
        result = await _engine().evaluate_async(
            script="""
attachment_create(
    content=b"\\x89PNG\\r\\n\\x1a\\n\\x00\\xff\\xfe",
    filename="binary.bin",
    description="Binary file",
    mime_type="text/plain",
)["id"]
""",
            execution_context=_script_context(db_engine, attachment_registry),
        )

        content = await attachment_registry.get_attachment_content(
            Database(engine=db_engine), result, acting_user_id=None
        )
        assert content == b"\x89PNG\r\n\x1a\n\x00\xff\xfe"

    async def test_script_create_json_attachment(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test creating a JSON attachment from within a script."""
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

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
        verify_context = Database(engine=db_engine)
        content = await attachment_registry.get_attachment_content(
            verify_context, attachment_id, acting_user_id=None
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
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

        script = """
attachment_id = attachment_create(
    content="Test content",
    filename="test.txt",
    description="Test file",
    mime_type="text/plain"
)["id"]

{"id": attachment_id, "got": attachment_get(attachment_id)}
"""

        result = await engine.evaluate_async(
            script=script,
            execution_context=execution_context,
        )

        assert result["got"] is not None
        assert result["got"]["attachment_id"] == result["id"]
        assert result["got"]["description"] == "Test file"
        assert result["got"]["source_type"] == "script"

    async def test_script_created_attachment_is_reachable_from_another_conversation(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        engine = _engine()
        attachment_id = await engine.evaluate_async(
            script="""
attachment_create(
    content="Test content",
    filename="test.txt",
    description="Test file",
    mime_type="text/plain",
)["id"]
""",
            execution_context=_script_context(
                db_engine, attachment_registry, conversation_id="conversation_a"
            ),
        )

        result = await engine.evaluate_async(
            script=f'attachment_get("{attachment_id}")',
            execution_context=_script_context(
                db_engine, attachment_registry, conversation_id="conversation_b"
            ),
        )

        assert result["attachment_id"] == attachment_id
        assert result["conversation_id"] == "conversation_a"

    async def test_script_read_bytes_text_attachment(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        sample_attachment: str,
    ) -> None:
        """Test reading attachment bytes from within a script."""
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

        script = f"""
raw = attachment_read_bytes("{sample_attachment}")
raw
"""

        result = await engine.evaluate_async(
            script=script,
            execution_context=execution_context,
        )

        assert isinstance(result, bytes)
        assert result == b"Test attachment content"

    async def test_script_read_bytes_binary_attachment(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that attachment_read_bytes preserves non-UTF-8 binary data through Monty."""
        # PNG header followed by bytes that are invalid UTF-8
        binary_payload = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x80\x81"

        db_context = Database(engine=db_engine)
        attachment_record = await attachment_registry.register_user_attachment(
            db_context=db_context,
            content=binary_payload,
            mime_type="image/png",
            filename="test.png",
            conversation_id="test_conversation",
            user_id="test_user",
            description="Binary PNG attachment",
        )
        attachment_id = attachment_record.attachment_id

        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

        script = f"""
raw = attachment_read_bytes("{attachment_id}")
raw
"""

        result = await engine.evaluate_async(
            script=script,
            execution_context=execution_context,
        )

        assert isinstance(result, bytes)
        assert result == binary_payload

    async def test_script_read_bytes_returns_none_for_missing(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test that attachment_read_bytes returns None for non-existent attachment."""
        db_context = Database(engine=db_engine)
        execution_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            plugins=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        config = ScriptConfig(enable_print=True)
        engine = MontyEngine(
            config=config, default_timezone=ZoneInfo("Australia/Sydney")
        )

        script = """
fake_id = "00000000-0000-0000-0000-000000000000"
result = attachment_read_bytes(fake_id)
result == None
"""

        result = await engine.evaluate_async(
            script=script,
            execution_context=execution_context,
        )

        assert result is True


_CREATE_DERIVED_ATTACHMENT = """
attachment_create(
    content="derived from the web",
    filename="derived.txt",
    description="derived",
    mime_type="text/plain",
)["id"]
"""


def _external_tracker() -> InMemoryTurnTaintTracker:
    return InMemoryTurnTaintTracker(
        TurnTaintState.empty().add_source(
            TaintSource(
                source_type=TaintSourceType.TOOL_OUTPUT,
                source_id="web",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="read the web",
            )
        )
    )


async def test_a_script_created_attachment_carries_the_turns_taint(
    db_engine: AsyncEngine,
    attachment_registry: AttachmentRegistry,
) -> None:
    attachment_id = await _engine().evaluate_async(
        script=_CREATE_DERIVED_ATTACHMENT,
        execution_context=_script_context(
            db_engine, attachment_registry, taint_tracker=_external_tracker()
        ),
    )

    stored = await attachment_registry.get_attachment(
        Database(engine=db_engine), attachment_id, acting_user_id=None
    )
    assert stored is not None
    assert (
        TurnTaintState.from_metadata(stored.metadata.get("taint_metadata")).max_tier
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.parametrize(
    ("read_function", "expected_content"),
    [
        ("attachment_read", "derived from the web"),
        ("attachment_read_bytes", b"derived from the web"),
    ],
)
async def test_a_script_reading_an_external_attachment_raises_its_turn(
    db_engine: AsyncEngine,
    attachment_registry: AttachmentRegistry,
    read_function: str,
    expected_content: str | bytes,
) -> None:
    """A clean script cannot launder external content through an attachment."""
    engine = _engine()
    attachment_id = await engine.evaluate_async(
        script=_CREATE_DERIVED_ATTACHMENT,
        execution_context=_script_context(
            db_engine, attachment_registry, taint_tracker=_external_tracker()
        ),
    )
    reader_tracker = InMemoryTurnTaintTracker()

    content = await engine.evaluate_async(
        script=f'{read_function}("{attachment_id}")',
        execution_context=_script_context(
            db_engine, attachment_registry, taint_tracker=reader_tracker
        ),
    )

    assert content == expected_content
    assert reader_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
