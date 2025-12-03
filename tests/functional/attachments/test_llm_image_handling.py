"""
Integration test for LLM image handling in ProcessingService using real database fixtures.
Verifies that attachment references (URLs) are correctly hydrated into accessible data URIs
before being sent to the LLM client.
"""

import base64
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.messages import LLMMessage, UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolAttachment


class MockLLMClient:
    """Mock LLM Client that behaves like a real one but captures requests."""

    def __init__(self) -> None:
        self.captured_messages: list[LLMMessage] = []

    async def generate_response_stream(
        self,
        messages: list[LLMMessage],
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        self.captured_messages = list(messages)  # Make a copy
        # Yield a simple content event
        yield LLMStreamEvent(type="content", content="I see the image.")
        # Yield done event with metadata
        yield LLMStreamEvent(
            type="done",
            metadata={"message": {"role": "assistant", "content": "I see the image."}},
        )

    def create_attachment_injection(self, attachment: ToolAttachment) -> UserMessage:
        # Return a simple UserMessage for the injection part
        return UserMessage(
            content=f"[System: Attachment {attachment.attachment_id} injected]"
        )


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.mark.asyncio
async def test_image_handling_with_real_db(
    db_engine: AsyncEngine, mock_llm: MockLLMClient
) -> None:
    """
    End-to-end integration test for image handling in chat interactions.

    1. Sets up a real AttachmentRegistry backed by the test database (sqlite/postgres).
    2. Stores a real file in the registry.
    3. Invokes handle_chat_interaction with an internal URL pointing to that file.
    4. Verifies the LLM client received the full base64 Data URI, not the internal URL.
    """

    # 1. Setup temp storage for attachments
    temp_storage_dir = tempfile.mkdtemp()
    try:
        # Initialize registry with real DB engine
        registry = AttachmentRegistry(
            storage_path=temp_storage_dir, db_engine=db_engine, config=None
        )

        # 2. Create and store a real attachment
        # Using a unique conversation ID to ensure isolation
        conversation_id = f"conv_{uuid.uuid4().hex}"
        image_content = b"\xff\xd8\xff\xe0\x00\x10JFIF..."  # Fake JPEG header
        filename = "test_image.jpg"

        # Use a database context for registration
        async with DatabaseContext(engine=db_engine) as db_context:
            metadata = await registry.register_user_attachment(
                db_context=db_context,
                content=image_content,
                filename=filename,
                mime_type="image/jpeg",
                description="Test Image",
                user_id="test_user",
                conversation_id=conversation_id,
            )

        assert metadata.attachment_id is not None

        # 3. Setup ProcessingService
        tools_provider = AsyncMock()
        tools_provider.get_tool_definitions.return_value = []  # No tools needed

        config = ProcessingServiceConfig(
            id="test_profile",
            prompts={
                "system_prompt": "You are a test assistant."
            },  # Corrected quote escaping
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
        )

        service = ProcessingService(
            llm_client=mock_llm,  # type: ignore
            tools_provider=tools_provider,
            service_config=config,
            context_providers=[],
            server_url="http://localhost:8000",
            app_config={},
            attachment_registry=registry,  # Provide the real registry
        )

        # 4. Simulate a user message containing an internal attachment URL
        # This mirrors what the Telegram/Web handlers produce
        internal_url = f"/api/attachments/{metadata.attachment_id}"
        trigger_content = [{"type": "image_url", "image_url": {"url": internal_url}}]

        # Use a database context for the interaction
        async with DatabaseContext(engine=db_engine) as db_context:
            await service.handle_chat_interaction(
                db_context=db_context,
                interface_type="telegram",
                conversation_id=conversation_id,
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                trigger_content_parts=trigger_content,  # type: ignore
                trigger_interface_message_id="msg_123",
                user_name="TestUser",
            )

        # 5. Verify what the LLM received
        assert len(mock_llm.captured_messages) > 0

        # Find the user message (should be the last one)
        last_message = mock_llm.captured_messages[-1]
        assert isinstance(last_message, UserMessage)

        # Inspect content parts
        # Expected: List of parts, where one is the image with data URI
        content = last_message.content
        assert isinstance(content, list), f"Expected list content, got {type(content)}"

        # Check for ImageUrlContentPart object
        image_part = next(
            (p for p in content if hasattr(p, "type") and p.type == "image_url"), None
        )

        assert image_part is not None, "LLM did not receive an image_url part"

        sent_url = image_part.image_url["url"]

        # CRITICAL ASSERTION: The URL must be a data URI, NOT the internal API URL
        # This confirms the _convert_attachment_urls_to_data_uris logic ran
        assert sent_url.startswith("data:image/jpeg;base64,"), (
            f"Regression! LLM received internal URL instead of Data URI: {sent_url}"
        )

        # Verify content integrity
        base64_data = sent_url.split(",")[1]
        decoded_data = base64.b64decode(base64_data)
        assert decoded_data == image_content, "Image data corrupted during conversion"

    finally:
        # Cleanup temp storage
        shutil.rmtree(temp_storage_dir)
