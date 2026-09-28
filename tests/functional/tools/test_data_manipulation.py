"""Functional tests for data manipulation tools.

Tests jq_query tool by calling it from scripts (realistic usage)
rather than direct Python calls.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools import AVAILABLE_FUNCTIONS, TOOLS_DEFINITION
from family_assistant.tools.data_manipulation import jq_query_tool
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


def _create_processing_service() -> ProcessingService:
    """Create a real processing service with tools provider for script execution."""
    # Create tools provider with all available tools
    tools_provider = LocalToolsProvider(
        definitions=TOOLS_DEFINITION,
        implementations=AVAILABLE_FUNCTIONS,
    )

    # Create minimal service config
    service_config = ProcessingServiceConfig(
        prompts={},
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="test_data_manipulation",
    )

    # Create mock LLM client (not used in these tests but required by ProcessingService)
    llm_client = RuleBasedMockLLMClient(rules=[], default_response=None)

    # ast-grep-ignore: no-dict-any - Test code
    dummy_app_config = AppConfig()

    # Create real ProcessingService with minimal dependencies
    return ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config=dummy_app_config,
    )


async def _store_test_attachment(
    registry: AttachmentRegistry,
    db_context: Database,
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
    db_context = Database(db_engine)
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
    """Test the jq_query tool functionality via script execution."""

    @pytest.mark.parametrize(
        ("jq_program", "expected_data"),
        [
            (
                ".items",
                [
                    {"id": 1, "name": "Alice", "age": 30, "city": "New York"},
                    {"id": 2, "name": "Bob", "age": 25, "city": "Los Angeles"},
                    {"id": 3, "name": "Charlie", "age": 35, "city": "Chicago"},
                ],
            ),
            (".items | length", 3),
            (".items[0]", {"id": 1, "name": "Alice", "age": 30, "city": "New York"}),
            (".items | map(.name)", ["Alice", "Bob", "Charlie"]),
            ("[.items[0].id, .items[-1].id]", [1, 3]),
        ],
        ids=["all_items", "count", "first_item", "map_field", "first_and_last_id"],
    )
    async def test_jq_query_basic(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
        jq_program: str,
        expected_data: object,
    ) -> None:
        """Test jq queries returning list, scalar, dict and mixed-list shapes."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        db_context = Database(db_engine)
        processing_service = _create_processing_service()
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id=conversation_id,
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            attachment_registry=registry,
            processing_service=processing_service,
            clock=None,
            home_assistant_client=None,
            event_sources={},
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        script = f'''
result = jq_query(
    attachment_id="{attachment_id}",
    jq_program="{jq_program}"
)
result
            '''

        result = await execute_script_tool(exec_context, script=script)

        # Script execution should succeed
        assert result.text is not None
        assert "Error" not in result.text

        assert result.get_data() == expected_data

    async def test_jq_query_invalid_program(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test jq query with invalid jq syntax."""
        registry, attachment_id, conversation_id = attachment_registry_with_json

        db_context = Database(db_engine)
        processing_service = _create_processing_service()
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id=conversation_id,
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            attachment_registry=registry,
            processing_service=processing_service,
            clock=None,
            home_assistant_client=None,
            event_sources={},
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        # Query: invalid jq syntax (via script)
        script = f'''
result = jq_query(
    attachment_id="{attachment_id}",
    jq_program="invalid jq syntax ["
)
result
            '''

        result = await execute_script_tool(exec_context, script=script)

        # Script should report the specific jq compilation error
        assert result.text is not None
        assert "invalid jq query" in result.text.lower()

    async def test_jq_query_attachment_not_found(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """Test jq query with a well-formed but unregistered attachment UUID.

        Called directly rather than via a script: the script layer's own
        argument-resolution validates and looks up the attachment before
        jq_query ever runs, so a request routed through a script never
        reaches this tool-level "not found" branch.
        """
        storage_path = tmp_path / "attachments"
        storage_path.mkdir(parents=True, exist_ok=True)

        registry = AttachmentRegistry(
            storage_path=str(storage_path), db_engine=db_engine, config=None
        )

        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        missing_attachment_id = str(uuid.uuid4())
        result = await jq_query_tool(
            exec_context, attachment_id=missing_attachment_id, jq_program=".items"
        )

        assert result.text is not None
        assert "not found" in result.text.lower()
        assert missing_attachment_id in result.text

    async def test_jq_query_cross_conversation_access_allowed(
        self,
        db_engine: AsyncEngine,
        attachment_registry_with_json: tuple[AttachmentRegistry, str, str],
    ) -> None:
        """Test that jq query allows cross-conversation attachment access."""
        registry, attachment_id, _conversation_id = attachment_registry_with_json

        db_context = Database(db_engine)
        # Try to access from a different conversation
        processing_service = _create_processing_service()
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="different_conversation",
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            attachment_registry=registry,
            processing_service=processing_service,
            clock=None,
            home_assistant_client=None,
            event_sources={},
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        # Try to query attachment from different conversation (via script)
        script = f'''
result = jq_query(
    attachment_id="{attachment_id}",
    jq_program=".items"
)
result
            '''

        result = await execute_script_tool(exec_context, script=script)

        # Script execution should succeed
        assert result.text is not None
        assert "Error" not in result.text

        # Parse the result data
        data = result.get_data()
        assert isinstance(data, list)
        assert len(data) == 3

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

        db_context = Database(db_engine)
        # Store attachment using helper
        attachment_id = await _store_test_attachment(
            registry=registry,
            db_context=db_context,
            content=text_content,
            filename="test.txt",
            mime_type="text/plain",
            conversation_id=conversation_id,
        )

        processing_service = _create_processing_service()
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id=conversation_id,
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            attachment_registry=registry,
            processing_service=processing_service,
            clock=None,
            home_assistant_client=None,
            event_sources={},
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        # Try to query non-JSON attachment (via script)
        script = f'''
result = jq_query(
    attachment_id="{attachment_id}",
    jq_program=".items"
)
result
            '''

        result = await execute_script_tool(exec_context, script=script)

        # Script should report that the attachment content isn't valid JSON
        assert result.text is not None
        assert "not valid json" in result.text.lower()

    async def test_jq_query_no_attachment_registry(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test jq query when attachment registry is not available.

        Called directly rather than via a script: the "attachment_id"
        parameter is declared as an attachment type, so a script-routed
        call gets rejected by argument resolution ("Attachment registry
        not available in execution context" via a different, wrapped
        error) before jq_query's own registry check ever runs.
        """
        db_context = Database(db_engine)
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            attachment_registry=None,  # No registry
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources={},
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await jq_query_tool(
            exec_context, attachment_id="some-id", jq_program=".items"
        )

        assert result.text == "Error: Attachment registry not available."
