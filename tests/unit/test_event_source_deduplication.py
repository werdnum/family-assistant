"""Test that event sources are properly deduplicated."""

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from family_assistant.assistant import Assistant


class TestEventSourceDeduplication(unittest.TestCase):
    """Test event source deduplication in Assistant."""

    @patch("family_assistant.assistant.init_db")
    @patch("family_assistant.assistant.storage.init_vector_db")
    @patch("family_assistant.assistant.create_home_assistant_client")
    async def test_single_event_source_per_ha_instance(
        self,
        mock_create_ha_client: Any,
        mock_init_vector_db: Any,
        mock_init_db: Any,
    ) -> None:
        """Test that only one event source is created per unique HA instance."""
        # Mock the HA client creation
        mock_ha_client = MagicMock()
        mock_create_ha_client.return_value = mock_ha_client

        # Configure test with multiple profiles using same HA instance
        config = {
            "telegram_token": "test_token",
            "allowed_user_ids": [12345],
            "developer_chat_id": 12345,
            "model": "test-model",
            "embedding_model": "mock-deterministic-embedder",
            "embedding_dimensions": 384,
            "server_url": "http://test.local",
            "event_system": {
                "enabled": True,
                "sources": {"home_assistant": {"enabled": True}},
            },
            "service_profiles": [
                {
                    "id": "profile1",
                    "processing_config": {
                        "home_assistant_api_url": "http://ha.local",
                        "home_assistant_token": "test_token",
                        "home_assistant_verify_ssl": True,
                        "prompts": {},
                        "calendar_config": {},
                        "timezone": "UTC",
                        "max_history_messages": 10,
                        "history_max_age_hours": 24,
                    },
                    "tools_config": {},
                },
                {
                    "id": "profile2",
                    "processing_config": {
                        "home_assistant_api_url": "http://ha.local",
                        "home_assistant_token": "test_token",
                        "home_assistant_verify_ssl": True,
                        "prompts": {},
                        "calendar_config": {},
                        "timezone": "UTC",
                        "max_history_messages": 10,
                        "history_max_age_hours": 24,
                    },
                    "tools_config": {},
                },
            ],
        }

        # Create assistant and set up dependencies
        assistant = Assistant(config)

        # Mock other dependencies to avoid full initialization
        with (
            patch("family_assistant.assistant.LiteLLMClient"),
            patch("family_assistant.assistant.LocalToolsProvider"),
            patch("family_assistant.assistant.MCPToolsProvider"),
            patch("family_assistant.assistant.CompositeToolsProvider"),
            patch("family_assistant.assistant.ConfirmingToolsProvider"),
            patch("family_assistant.assistant.ProcessingService"),
            patch("family_assistant.assistant.PlaywrightScraper"),
            patch("family_assistant.assistant.DocumentIndexer"),
            patch("family_assistant.assistant.EmailIndexer"),
            patch("family_assistant.assistant.NotesIndexer"),
            patch("family_assistant.assistant.TelegramService"),
            patch("family_assistant.assistant.EventProcessor") as mock_event_processor,
        ):
            # Mock get_tool_definitions to avoid MCP initialization
            mock_tools_provider = AsyncMock()
            mock_tools_provider.get_tool_definitions = AsyncMock(return_value=[])

            await assistant.setup_dependencies()

            # Verify EventProcessor was called with correct number of sources
            assert mock_event_processor.called
            call_args = mock_event_processor.call_args
            sources = call_args[1]["sources"]

            # Should have exactly one event source
            assert len(sources) == 1
            assert "home_assistant" in sources

            # Verify only one HA client was created (both profiles use same URL/token)
            assert mock_create_ha_client.call_count == 1
