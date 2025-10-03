"""Tests for Telegram thread history functionality.

This module tests that thread history queries correctly include the root message
and all child messages in a thread.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext


@pytest.mark.asyncio
async def test_thread_history_includes_root_message(db_engine: AsyncEngine) -> None:
    """
    Test that get_by_thread_id includes the root message itself.

    When querying for messages in a thread, the root message (which has
    thread_root_id=NULL) should be included along with all child messages
    (which have thread_root_id pointing to the root).
    """
    async with DatabaseContext(engine=db_engine) as db:
        # Create a root message (thread_root_id will be NULL initially)
        root_msg = await db.message_history.add(
            interface_type="telegram",
            conversation_id="test_chat_123",
            interface_message_id="100",
            turn_id=None,
            thread_root_id=None,  # Root message has no thread_root_id yet
            role="user",
            content="Can you highlight the eagle statue?",
            timestamp=datetime.now(timezone.utc),
            tool_call_id=None,
            tool_calls=None,
            reasoning_info=None,
            error_traceback=None,
            processing_profile_id="default_assistant",
            attachments=None,
            tool_name=None,
            provider_metadata=None,
        )

        assert root_msg is not None, "Failed to create root message"
        root_internal_id = root_msg["internal_id"]

        # Create child messages in the same thread
        assistant_msg = await db.message_history.add(
            interface_type="telegram",
            conversation_id="test_chat_123",
            interface_message_id="101",
            turn_id="turn_1",
            thread_root_id=root_internal_id,  # Points to root
            role="assistant",
            content="I'll get a camera snapshot for you.",
            timestamp=datetime.now(timezone.utc),
            tool_call_id=None,
            tool_calls=[
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_camera_snapshot",
                        "arguments": '{"camera_entity_id": "camera.test"}',
                    },
                }
            ],
            reasoning_info=None,
            error_traceback=None,
            processing_profile_id="default_assistant",
            attachments=None,
            tool_name=None,
            provider_metadata=None,
        )

        assert assistant_msg is not None, "Failed to create assistant message"

        tool_msg = await db.message_history.add(
            interface_type="telegram",
            conversation_id="test_chat_123",
            interface_message_id=None,  # Tool messages don't have interface IDs
            turn_id="turn_1",
            thread_root_id=root_internal_id,  # Points to root
            role="tool",
            content="Retrieved snapshot from camera\n[Attachment ID: abc-123-def]",
            timestamp=datetime.now(timezone.utc),
            tool_call_id="call_123",
            tool_calls=None,
            reasoning_info=None,
            error_traceback=None,
            processing_profile_id="default_assistant",
            attachments=[
                {
                    "type": "tool_result",
                    "attachment_id": "abc-123-def",
                    "mime_type": "image/jpeg",
                }
            ],
            tool_name="get_camera_snapshot",
            provider_metadata=None,
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
        assert thread_messages[0]["internal_id"] == root_internal_id
        assert thread_messages[0]["role"] == "user"
        assert thread_messages[0]["content"] == "Can you highlight the eagle statue?"
        assert (
            thread_messages[0]["thread_root_id"] is None
        )  # Root has no thread_root_id

        # Verify child messages follow
        assert thread_messages[1]["internal_id"] == assistant_msg["internal_id"]
        assert thread_messages[1]["role"] == "assistant"
        assert thread_messages[1]["thread_root_id"] == root_internal_id

        assert thread_messages[2]["internal_id"] == tool_msg["internal_id"]
        assert thread_messages[2]["role"] == "tool"
        assert thread_messages[2]["thread_root_id"] == root_internal_id
        assert "[Attachment ID: abc-123-def]" in thread_messages[2]["content"]


@pytest.mark.asyncio
async def test_thread_history_with_profile_filter(db_engine: AsyncEngine) -> None:
    """
    Test that get_by_thread_id correctly filters by processing_profile_id.

    Threads can have messages from different profiles (e.g., when switching
    profiles via delegation). The query should only return messages from the
    specified profile.
    """
    async with DatabaseContext(engine=db_engine) as db:
        # Create root message with profile A
        root_msg = await db.message_history.add(
            interface_type="telegram",
            conversation_id="test_chat_456",
            interface_message_id="200",
            turn_id=None,
            thread_root_id=None,
            role="user",
            content="Test message",
            timestamp=datetime.now(timezone.utc),
            tool_call_id=None,
            tool_calls=None,
            reasoning_info=None,
            error_traceback=None,
            processing_profile_id="profile_a",
            attachments=None,
            tool_name=None,
            provider_metadata=None,
        )

        assert root_msg is not None, "Failed to create root message"
        root_internal_id = root_msg["internal_id"]

        # Create child message with profile A
        await db.message_history.add(
            interface_type="telegram",
            conversation_id="test_chat_456",
            interface_message_id="201",
            turn_id="turn_1",
            thread_root_id=root_internal_id,
            role="assistant",
            content="Response from profile A",
            timestamp=datetime.now(timezone.utc),
            tool_call_id=None,
            tool_calls=None,
            reasoning_info=None,
            error_traceback=None,
            processing_profile_id="profile_a",
            attachments=None,
            tool_name=None,
            provider_metadata=None,
        )

        # Create child message with profile B
        await db.message_history.add(
            interface_type="telegram",
            conversation_id="test_chat_456",
            interface_message_id="202",
            turn_id="turn_2",
            thread_root_id=root_internal_id,
            role="assistant",
            content="Response from profile B",
            timestamp=datetime.now(timezone.utc),
            tool_call_id=None,
            tool_calls=None,
            reasoning_info=None,
            error_traceback=None,
            processing_profile_id="profile_b",
            attachments=None,
            tool_name=None,
            provider_metadata=None,
        )

        # Query for profile A messages only
        profile_a_messages = await db.message_history.get_by_thread_id(
            thread_root_id=root_internal_id, processing_profile_id="profile_a"
        )

        # Should include root (profile_a) and first child (profile_a), but not second child (profile_b)
        assert len(profile_a_messages) == 2
        assert all(
            msg["processing_profile_id"] == "profile_a" for msg in profile_a_messages
        )
        assert profile_a_messages[0]["role"] == "user"
        assert profile_a_messages[1]["content"] == "Response from profile A"

        # Query for profile B messages only
        profile_b_messages = await db.message_history.get_by_thread_id(
            thread_root_id=root_internal_id, processing_profile_id="profile_b"
        )

        # Should only include the second child (profile_b), not root or first child
        assert len(profile_b_messages) == 1
        assert profile_b_messages[0]["content"] == "Response from profile B"


@pytest.mark.asyncio
async def test_empty_thread_returns_empty_list(db_engine: AsyncEngine) -> None:
    """Test that querying a non-existent thread returns an empty list."""
    async with DatabaseContext(engine=db_engine) as db:
        # Query for a thread that doesn't exist
        messages = await db.message_history.get_by_thread_id(
            thread_root_id=99999  # Non-existent ID
        )

        assert messages == []
