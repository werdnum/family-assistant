"""Test that event sources are properly deduplicated."""

from unittest.mock import MagicMock, patch

import pytest

from family_assistant.assistant import Assistant


class TestEventSourceDeduplication:
    """Test event source deduplication in Assistant."""

    @pytest.mark.asyncio
    async def test_single_event_source_per_ha_instance(self) -> None:
        """Test that only one event source is created per unique HA instance."""
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

        # Only mock the external Home Assistant client creation
        with patch(
            "family_assistant.assistant.create_home_assistant_client"
        ) as mock_create_ha_client:
            # Mock the HA client creation
            mock_ha_client = MagicMock()
            mock_create_ha_client.return_value = mock_ha_client

            # Create assistant with real dependencies
            assistant = Assistant(config)
            await assistant.setup_dependencies()

            try:
                # The important test: only one HA client was created despite two profiles using same URL/token
                assert mock_create_ha_client.call_count == 1

                # Verify the event processor was set up correctly
                assert assistant.event_processor is not None
                assert (
                    len(assistant.event_processor.sources) == 2
                )  # home_assistant and indexing

            finally:
                # Clean up to avoid side effects
                # Stop the event processor if it's running
                if hasattr(assistant, "event_processor") and assistant.event_processor:
                    await assistant.event_processor.stop()

                # Close the httpx client
                if (
                    hasattr(assistant, "shared_httpx_client")
                    and assistant.shared_httpx_client
                ):
                    await assistant.shared_httpx_client.aclose()

                # Stop telegram service if it exists
                if (
                    hasattr(assistant, "telegram_service")
                    and assistant.telegram_service
                    and hasattr(assistant.telegram_service, "application")
                ):
                    try:
                        await assistant.telegram_service.application.stop()
                        await assistant.telegram_service.application.shutdown()
                    except Exception:
                        pass  # Ignore errors during cleanup
