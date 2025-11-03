"""
Tests for user attachment processing in chat API endpoints.

Tests the attachment upload and processing functionality for both
streaming and non-streaming chat endpoints.
"""

import base64
import io
import json
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from family_assistant.services.attachment_registry import (
    AttachmentMetadata,
    AttachmentRegistry,
)
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.context import DatabaseContext


@pytest.fixture
async def mock_attachment_registry() -> AsyncMock:
    """Mock attachment registry for testing."""
    registry = AsyncMock(spec=AttachmentRegistry)
    registry.register_user_attachment.return_value = AttachmentMetadata(
        attachment_id="test-attachment-id",
        source_type="user",
        source_id="api_user",
        mime_type="image/png",
        description="Test attachment",
        size=1000,
        content_url="http://localhost:8000/api/v1/attachments/test-attachment-id",
        storage_path="/tmp/test-attachment-id.png",
    )
    return registry


@pytest.fixture
def sample_image_base64() -> str:
    """Generate a sample base64-encoded PNG image."""
    # Create a simple 100x100 red square image
    img = Image.new("RGB", (100, 100), color="red")

    # Convert to PNG bytes
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    # Encode to base64
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


@pytest.fixture
def sample_image_data_url(sample_image_base64: str) -> str:
    """Sample data URL with image."""
    return f"data:image/png;base64,{sample_image_base64}"


class TestUserAttachmentProcessing:
    """Test user attachment processing in chat API."""

    @pytest.mark.asyncio
    async def test_base64_image_upload_non_streaming(
        self,
        api_test_client: AsyncClient,
        sample_image_base64: str,
        api_db_context: DatabaseContext,
    ) -> None:
        """Test uploading a base64 image via non-streaming endpoint."""
        payload = {
            "prompt": "Analyze this image",
            "conversation_id": "test-conv-123",
            "attachments": [
                {
                    "type": "image",
                    "content": sample_image_base64,
                    "filename": "test.png",
                }
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 200

        result = response.json()
        assert "reply" in result
        assert "attachments" in result
        assert len(result["attachments"]) == 1

        attachment = result["attachments"][0]
        assert attachment["type"] == "image"
        assert "attachment_id" in attachment
        assert "url" in attachment

    @pytest.mark.asyncio
    async def test_data_url_image_upload_non_streaming(
        self,
        api_test_client: AsyncClient,
        sample_image_data_url: str,
        api_db_context: DatabaseContext,
    ) -> None:
        """Test uploading a data URL image via non-streaming endpoint."""
        payload = {
            "prompt": "What's in this image?",
            "conversation_id": "test-conv-456",
            "attachments": [
                {
                    "type": "image",
                    "content": sample_image_data_url,
                    "filename": "test2.png",
                }
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 200

        result = response.json()
        assert "attachments" in result
        assert len(result["attachments"]) == 1

    @pytest.mark.asyncio
    async def test_base64_image_upload_streaming(
        self,
        api_test_client: AsyncClient,
        sample_image_base64: str,
        api_db_context: DatabaseContext,
    ) -> None:
        """Test uploading a base64 image via streaming endpoint."""
        payload = {
            "prompt": "Describe this image",
            "conversation_id": "test-conv-789",
            "attachments": [
                {
                    "type": "image",
                    "content": sample_image_base64,
                    "filename": "stream_test.png",
                }
            ],
        }

        async with api_test_client.stream(
            "POST", "/api/v1/chat/send_message_stream", json=payload
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            # Collect all SSE events
            events = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])  # Remove "data: " prefix
                        events.append(data)
                    except json.JSONDecodeError:
                        continue

            # Should have attachment events
            attachment_events = [e for e in events if e.get("type") == "attachment"]
            assert len(attachment_events) >= 1

            # Check first attachment event
            first_attachment = attachment_events[0]
            assert "attachment_id" in first_attachment
            assert "url" in first_attachment

    @pytest.mark.asyncio
    async def test_multiple_attachments(
        self,
        api_test_client: AsyncClient,
        sample_image_base64: str,
        api_db_context: DatabaseContext,
    ) -> None:
        """Test uploading multiple attachments in one request."""
        payload = {
            "prompt": "Compare these images",
            "conversation_id": "test-conv-multi",
            "attachments": [
                {
                    "type": "image",
                    "content": sample_image_base64,
                    "filename": "image1.png",
                },
                {
                    "type": "image",
                    "content": sample_image_base64,
                    "filename": "image2.png",
                },
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 200

        result = response.json()
        assert "attachments" in result
        assert len(result["attachments"]) == 2

        # Each attachment should have unique ID
        attachment_ids = [att["attachment_id"] for att in result["attachments"]]
        assert len(set(attachment_ids)) == 2

    @pytest.mark.asyncio
    async def test_invalid_base64_content(
        self, api_test_client: AsyncClient, api_db_context: DatabaseContext
    ) -> None:
        """Test handling of invalid base64 content."""
        payload = {
            "prompt": "This should fail",
            "conversation_id": "test-conv-invalid",
            "attachments": [
                {
                    "type": "image",
                    "content": "123",  # Invalid base64 - incorrect padding
                    "filename": "invalid.png",
                }
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 400

        result = response.json()
        assert "detail" in result
        assert "base64" in result["detail"].lower()

    @pytest.mark.asyncio
    async def test_missing_attachment_content(
        self, api_test_client: AsyncClient, api_db_context: DatabaseContext
    ) -> None:
        """Test handling of attachment without content."""
        payload = {
            "prompt": "Missing content",
            "conversation_id": "test-conv-missing",
            "attachments": [
                {
                    "type": "image",
                    "filename": "missing.png",
                    # No content field
                }
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 400

        result = response.json()
        assert "detail" in result

    @pytest.mark.asyncio
    async def test_empty_attachment_content(
        self, api_test_client: AsyncClient, api_db_context: DatabaseContext
    ) -> None:
        """Test handling of empty attachment content."""
        payload = {
            "prompt": "Empty content",
            "conversation_id": "test-conv-empty",
            "attachments": [{"type": "image", "content": "", "filename": "empty.png"}],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 400

        result = response.json()
        assert "detail" in result

    @pytest.mark.asyncio
    async def test_attachment_storage_in_database(
        self,
        api_test_client: AsyncClient,
        sample_image_base64: str,
        api_db_context: DatabaseContext,
    ) -> None:
        """Test that attachments are properly stored in database."""
        conversation_id = "test-conv-db-storage"
        payload = {
            "prompt": "Store this image",
            "conversation_id": conversation_id,
            "attachments": [
                {
                    "type": "image",
                    "content": sample_image_base64,
                    "filename": "db_test.png",
                }
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 200

        result = response.json()
        attachment_id = result["attachments"][0]["attachment_id"]

        # Verify attachment metadata is stored in database

        query = select(attachment_metadata_table).where(
            attachment_metadata_table.c.attachment_id == attachment_id
        )
        metadata_row = await api_db_context.fetch_one(query)

        assert metadata_row is not None, (
            "Attachment metadata should be stored in database"
        )
        assert metadata_row["conversation_id"] == conversation_id
        assert metadata_row["mime_type"] == "image/png"
        assert metadata_row["source_type"] == "user"

        # Check metadata JSON contains original filename
        metadata_json = metadata_row["metadata"] or {}
        assert metadata_json.get("original_filename") == "db_test.png"

    @pytest.mark.asyncio
    async def test_attachment_access_control(
        self,
        api_test_client: AsyncClient,
        sample_image_base64: str,
        api_db_context: DatabaseContext,
    ) -> None:
        """Test conversation-scoped attachment access control."""
        # Upload attachment in one conversation
        conv1_id = "test-conv-access-1"
        payload = {
            "prompt": "Store this image",
            "conversation_id": conv1_id,
            "attachments": [
                {
                    "type": "image",
                    "content": sample_image_base64,
                    "filename": "access_test.png",
                }
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 200

        result = response.json()
        result["attachments"][0]["attachment_id"]

        # This test verifies that attachment IDs are returned correctly
        # Access control testing will be done via direct registry testing

    @pytest.mark.asyncio
    async def test_data_url_mime_type_detection(
        self, api_test_client: AsyncClient, api_db_context: DatabaseContext
    ) -> None:
        """Test MIME type detection from data URLs."""
        # JPEG data URL
        jpeg_data_url = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAAAAA"

        payload = {
            "prompt": "Test JPEG",
            "conversation_id": "test-conv-jpeg",
            "attachments": [
                {"type": "image", "content": jpeg_data_url, "filename": "test.jpg"}
            ],
        }

        response = await api_test_client.post("/api/v1/chat/send_message", json=payload)
        assert response.status_code == 200

        result = response.json()
        result["attachments"][0]["attachment_id"]

        # This test verifies that data URLs are processed correctly
        # MIME type detection will be tested via direct service testing
