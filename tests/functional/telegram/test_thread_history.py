"""Tests for Telegram thread history functionality.

This module tests that thread history queries correctly include the root message
and all child messages in a thread.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.database import Database
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest.mark.asyncio
async def test_thread_history_includes_root_message(db_engine: AsyncEngine) -> None:
    """
    Test that get_by_thread_id includes the root message itself.

    When querying for messages in a thread, the root message (which has
    thread_root_id=NULL) should be included along with all child messages
    (which have thread_root_id pointing to the root).
    """
    db = Database(engine=db_engine)
    # Create a root message (thread_root_id will be NULL initially)
    root_msg = await db.message_history.add_message(
        message=UserMessage.from_trusted_user(
            content="Can you highlight the eagle statue?"
        ),
        interface_type="telegram",
        conversation_id="test_chat_123",
        interface_message_id="100",
        processing_profile_id="default_assistant",
        timestamp=datetime.now(UTC),
    )

    assert root_msg is not None, "Failed to create root message"
    root_internal_id = root_msg

    # Create child messages in the same thread
    assistant_msg = await db.message_history.add_message(
        message=AssistantMessage(
            content="I'll get a camera snapshot for you.",
            tool_calls=[
                ToolCallItem(
                    id="call_123",
                    type="function",
                    function=ToolCallFunction(
                        name="get_camera_snapshot",
                        arguments='{"camera_entity_id": "camera.test"}',
                    ),
                )
            ],
        ),
        interface_type="telegram",
        conversation_id="test_chat_123",
        interface_message_id="101",
        turn_id="turn_1",
        thread_root_id=root_internal_id,
        processing_profile_id="default_assistant",
        timestamp=datetime.now(UTC),
    )

    assert assistant_msg is not None, "Failed to create assistant message"

    tool_msg = await db.message_history.add_message(
        message=ToolMessage(
            tool_call_id="call_123",
            content="Retrieved snapshot from camera\n[Attachment ID: abc-123-def]",
            name="get_camera_snapshot",
        ),
        interface_type="telegram",
        conversation_id="test_chat_123",
        turn_id="turn_1",
        thread_root_id=root_internal_id,
        processing_profile_id="default_assistant",
        timestamp=datetime.now(UTC),
        attachments=[
            {
                "type": "tool_result",
                "attachment_id": "abc-123-def",
                "mime_type": "image/jpeg",
            }
        ],
    )

    assert tool_msg is not None, "Failed to create tool message"

    # Query for thread messages
    thread_messages = await db.message_history.get_by_thread_id(
        thread_root_id=root_internal_id
    )

    # Verify all messages are returned, including the root
    assert len(thread_messages) == 3, (
        f"Expected 3 messages in thread, got {len(thread_messages)}"
    )

    # Verify the root message is first (due to timestamp ordering)
    assert thread_messages[0].role == "user"
    assert thread_messages[0].content == "Can you highlight the eagle statue?"

    # Verify child messages follow
    assert thread_messages[1].role == "assistant"

    assert thread_messages[2].role == "tool"
    assert "[Attachment ID: abc-123-def]" in thread_messages[2].content


@pytest.mark.asyncio
async def test_thread_history_with_profile_filter(db_engine: AsyncEngine) -> None:
    """
    Test that get_by_thread_id correctly filters by processing_profile_id.

    Threads can have messages from different profiles (e.g., when switching
    profiles via delegation). The query should only return messages from the
    specified profile.
    """
    db = Database(engine=db_engine)
    # Create root message with profile A
    root_msg = await db.message_history.add_message(
        message=UserMessage.from_trusted_user(content="Test message"),
        interface_type="telegram",
        conversation_id="test_chat_456",
        interface_message_id="200",
        processing_profile_id="profile_a",
        timestamp=datetime.now(UTC),
    )

    assert root_msg is not None, "Failed to create root message"
    root_internal_id = root_msg

    # Create child message with profile A
    await db.message_history.add_message(
        message=AssistantMessage(content="Response from profile A"),
        interface_type="telegram",
        conversation_id="test_chat_456",
        interface_message_id="201",
        turn_id="turn_1",
        thread_root_id=root_internal_id,
        processing_profile_id="profile_a",
        timestamp=datetime.now(UTC),
    )

    # Create child message with profile B
    await db.message_history.add_message(
        message=AssistantMessage(content="Response from profile B"),
        interface_type="telegram",
        conversation_id="test_chat_456",
        interface_message_id="202",
        turn_id="turn_2",
        thread_root_id=root_internal_id,
        processing_profile_id="profile_b",
        timestamp=datetime.now(UTC),
    )

    # Query for profile A messages only
    profile_a_messages = await db.message_history.get_by_thread_id(
        thread_root_id=root_internal_id, processing_profile_id="profile_a"
    )

    # Should include root (profile_a) and first child (profile_a), but not second child (profile_b)
    assert len(profile_a_messages) == 2
    assert profile_a_messages[0].role == "user"
    assert profile_a_messages[1].content == "Response from profile A"

    # Query for profile B messages only
    profile_b_messages = await db.message_history.get_by_thread_id(
        thread_root_id=root_internal_id, processing_profile_id="profile_b"
    )

    # Should only include the second child (profile_b), not root or first child
    assert len(profile_b_messages) == 1
    assert profile_b_messages[0].content == "Response from profile B"


@pytest.mark.asyncio
async def test_empty_thread_returns_empty_list(db_engine: AsyncEngine) -> None:
    """Test that querying a non-existent thread returns an empty list."""
    db = Database(engine=db_engine)
    # Query for a thread that doesn't exist
    messages = await db.message_history.get_by_thread_id(
        thread_root_id=99999  # Non-existent ID
    )

    assert messages == []


@pytest.mark.asyncio
async def test_attachment_context_extraction(db_engine: AsyncEngine) -> None:
    """
    Test that extract_conversation_context queries recent, in-conversation
    attachments and formats each one using its stored description.

    Confirms the conversation-id filter, the max-age cutoff, and that the
    rendered name comes from ``description`` rather than the original
    filename, by giving one attachment a description distinct from its
    filename and arranging attachments that must be excluded on each axis.
    """
    db = Database(engine=db_engine)
    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments", db_engine=db_engine
    )

    in_scope_1 = await attachment_registry.store_and_register_tool_attachment(
        file_content=b"\x89PNG\r\n\x1a\n",
        filename="IMG_0001.png",
        content_type="image/png",
        tool_name="get_camera_snapshot",
        description="bird_statue.png",
        conversation_id="test_chat_789",
        taint_state=TurnTaintState.empty(),
    )

    in_scope_2 = await attachment_registry.store_and_register_tool_attachment(
        file_content=b"PDF content here",
        filename="document.pdf",
        content_type="application/pdf",
        tool_name="attach_to_response",
        description="document.pdf",
        conversation_id="test_chat_789",
        taint_state=TurnTaintState.empty(),
    )

    other_conversation = await attachment_registry.store_and_register_tool_attachment(
        file_content=b"other convo",
        filename="other.png",
        content_type="image/png",
        tool_name="get_camera_snapshot",
        description="other_conversation_attachment.png",
        conversation_id="a_different_chat",
        taint_state=TurnTaintState.empty(),
    )

    too_old = await attachment_registry.store_and_register_tool_attachment(
        file_content=b"stale",
        filename="stale.png",
        content_type="image/png",
        tool_name="get_camera_snapshot",
        description="stale_attachment.png",
        conversation_id="test_chat_789",
        taint_state=TurnTaintState.empty(),
    )
    await db.execute(
        update(attachment_metadata_table)
        .where(attachment_metadata_table.c.attachment_id == too_old.attachment_id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=5))
    )

    service_config = ProcessingServiceConfig(
        prompts={
            "thread_attachments_context_header": "Recent Attachments in Conversation:\n{attachments_list}"
        },
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=2,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        id="test_profile",
    )

    llm_client = RuleBasedMockLLMClient(rules=[], default_response=None)
    local_provider = LocalToolsProvider(definitions=[], implementations={})
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    tools_provider = CompositeToolsProvider(providers=[local_provider, mcp_provider])

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=service_config,
        context_providers=[],
        server_url="http://localhost:8000",
        app_config=AppConfig(),
        attachment_registry=attachment_registry,
    )

    context = (
        await processing_service.attachment_processor.extract_conversation_context(
            db,
            conversation_id="test_chat_789",
            max_age_hours=2,
            prompts={},
            acting_user_id=None,
        )
    )

    assert context, "Expected non-empty attachment context"
    assert "Recent Attachments in Conversation:" in context
    assert in_scope_1.attachment_id in context
    assert in_scope_2.attachment_id in context
    assert "bird_statue.png" in context
    assert "document.pdf" in context
    assert "image/png" in context
    assert "application/pdf" in context
    assert "ago" in context.lower()

    assert other_conversation.attachment_id not in context
    assert "other_conversation_attachment.png" not in context
    assert too_old.attachment_id not in context
    assert "stale_attachment.png" not in context
