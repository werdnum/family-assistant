"""Test for jq_query() deadlock when called from scripts.

This test reproduces the deadlock that occurs when jq_query() is called from within
a script with a policy wrapper (which doesn't have get_raw_tool_definitions).
"""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.execute_script import execute_script_tool
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
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
    a script causes a 30-second timeout. The deadlock occurs because:

    1. _process_attachment_arguments() (async coroutine on main loop)
    2. → calls _get_raw_tool_definitions() (sync method)
    3. → which calls _run_async(get_tool_definitions()) with a policy wrapper
    4. → creates nested _run_async() call while already in async context
    5. → deadlock: main loop blocked, can't process the new coroutine
    """
    db = Database(engine=db_engine)
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)

    # Wrap it in PolicyEnforcingToolsProvider. This doesn't have
    # get_raw_tool_definitions(), which previously triggered the deadlock.
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=local_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
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
        camera_backend=None,
        processing_service=None,
        tools_provider=policy_provider,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
