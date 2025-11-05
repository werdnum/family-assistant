"""Test for jq_query() deadlock when called from scripts.

This test reproduces the deadlock that occurs when jq_query() is called from within
a Starlark script with a ConfirmingToolsProvider (which doesn't have get_raw_tool_definitions).
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import AVAILABLE_FUNCTIONS, TOOLS_DEFINITION
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    ConfirmingToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.fixture
async def attachment_registry(
    tmp_path: Path,
    db_engine: AsyncEngine,
) -> AttachmentRegistry:
    """Create an AttachmentRegistry for testing."""
    test_storage = tmp_path / "test_attachments"
    test_storage.mkdir(exist_ok=True)
    return AttachmentRegistry(
        storage_path=str(test_storage), db_engine=db_engine, config=None
    )


@pytest.mark.asyncio
async def test_jq_query_from_script_no_deadlock(
    db_engine: AsyncEngine,
    attachment_registry: AttachmentRegistry,
) -> None:
    """Test that jq_query() doesn't deadlock when called from a script.

    This test reproduces the deadlock issue where calling jq_query() from within
    a Starlark script causes a 30-second timeout. The deadlock occurs because:

    1. _process_attachment_arguments() (async coroutine on main loop)
    2. → calls _get_raw_tool_definitions() (sync method)
    3. → which calls _run_async(get_tool_definitions()) with ConfirmingToolsProvider
    4. → creates nested _run_async() call while already in async context
    5. → deadlock: main loop blocked, can't process the new coroutine
    """
    async with DatabaseContext(engine=db_engine) as db:
        # Create a basic LocalToolsProvider
        local_provider = LocalToolsProvider(
            definitions=TOOLS_DEFINITION,
            implementations=AVAILABLE_FUNCTIONS,
        )

        # Wrap it in ConfirmingToolsProvider
        # This doesn't have get_raw_tool_definitions(), triggering the deadlock
        confirming_provider = ConfirmingToolsProvider(
            wrapped_provider=local_provider,
            tools_requiring_confirmation=set(),  # No tools need confirmation, we just need the wrapper
        )

        ctx = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv",
            user_name="test",
            turn_id=None,
            db_context=db,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=attachment_registry,
            processing_service=None,
            tools_provider=confirming_provider,  # Use confirming provider
        )

        # Script that creates an attachment and then calls jq_query() on it
        # This will trigger the deadlock in _process_attachment_arguments
        script = """
# Create a JSON attachment (use text/plain since application/json isn't in allowed list)
test_data = [
    {"name": "Alice", "age": 30},
    {"name": "Bob", "age": 25},
    {"name": "Charlie", "age": 35}
]
attachment = attachment_create(
    content=json_encode(test_data),
    filename="test_data.json",
    description="Test data",
    mime_type="text/plain"
)

# Query the attachment to filter people over 30
# This is where the deadlock occurs
result = jq_query(
    attachment_id=attachment["id"],
    jq_program="[.[] | select(.age > 30)]"
)
result
"""

        # This should complete without timeout
        # Currently it will timeout after 30s due to the deadlock
        result = await execute_script_tool(ctx, script)

        # Verify the script executed successfully
        assert result.text is not None
        assert "Charlie" in result.text or "35" in result.text
