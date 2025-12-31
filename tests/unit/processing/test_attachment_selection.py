"""
Unit tests for attachment selection logic in ProcessingService.

Tests the _select_attachments_for_response method and related threshold logic.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from family_assistant.config_models import AppConfig
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentMetadata


class TestAttachmentSelectionThreshold:
    """Test attachment selection triggering threshold logic."""

    @pytest.fixture
    def mock_llm_client(self) -> MagicMock:
        """Create a mock LLM client."""
        return MagicMock()

    @pytest.fixture
    def mock_tools_provider(self) -> AsyncMock:
        """Create a mock tools provider."""
        return AsyncMock()

    @pytest.fixture
    def mock_attachment_registry(self) -> AsyncMock:
        """Create a mock attachment registry."""
        return AsyncMock()

    @pytest.fixture
    def app_config(self) -> AppConfig:
        """Create an app config with custom thresholds."""
        config = AppConfig()
        config.attachment_selection_threshold = 3
        config.max_response_attachments = 6
        return config

    @pytest.fixture
    def processing_service(
        self,
        mock_llm_client: MagicMock,
        mock_tools_provider: AsyncMock,
        mock_attachment_registry: AsyncMock,
        app_config: AppConfig,
    ) -> ProcessingService:
        """Create a ProcessingService for testing."""
        service_config = ProcessingServiceConfig(
            id="test_profile",
            prompts={"system": "Test system prompt"},
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
        )

        service = ProcessingService(
            llm_client=mock_llm_client,
            tools_provider=mock_tools_provider,
            service_config=service_config,
            context_providers=[],
            server_url=None,
            app_config=app_config,
        )
        service.attachment_registry = mock_attachment_registry
        return service

    @pytest.mark.asyncio
    async def test_attachment_selection_not_triggered_below_threshold(
        self, processing_service: ProcessingService
    ) -> None:
        """Test that selection is not triggered when attachment count is at or below threshold."""
        # Test with exactly 3 attachments (equal to threshold of 3)
        pending_attachment_ids = ["att1", "att2", "att3"]

        # Should return all attachments without calling selection
        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_attachment_ids,
            original_query="Test query",
        )

        # With threshold=3, having 3 attachments should not trigger selection
        # The logic is: if len > threshold (3), not if len >= threshold
        assert len(result) == 3
        assert set(result) == {"att1", "att2", "att3"}

    @pytest.mark.asyncio
    async def test_attachment_selection_triggered_above_threshold(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that selection is triggered when attachment count exceeds threshold."""
        # Test with 4 attachments (> threshold of 3)
        pending_attachment_ids = ["att1", "att2", "att3", "att4"]

        processing_service.attachment_registry = mock_attachment_registry

        # Create mock attachment metadata
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: AttachmentMetadata(
                attachment_id=att_id,
                source_type="tool",
                source_id="test_tool",
                mime_type="image/jpeg",
                description=f"Test attachment {att_id}",
                size=1024,
                created_at=datetime.now(),
            )
        )

        # Mock LLM response with tool call
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments={"attachment_ids": ["att1", "att3"]},
                        ),
                    )
                ],
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_attachment_ids,
            original_query="Test query",
        )

        # Should have called LLM for selection
        assert mock_llm_client.generate_response.called
        # Should return the LLM-selected attachments
        assert result == ["att1", "att3"]


class TestSelectAttachmentsForResponse:
    """Test the _select_attachments_for_response method."""

    @pytest.fixture
    def mock_llm_client(self) -> MagicMock:
        """Create a mock LLM client."""
        return MagicMock()

    @pytest.fixture
    def mock_attachment_registry(self) -> AsyncMock:
        """Create a mock attachment registry."""
        return AsyncMock()

    @pytest.fixture
    def app_config(self) -> AppConfig:
        """Create an app config with custom thresholds."""
        config = AppConfig()
        config.attachment_selection_threshold = 3
        config.max_response_attachments = 6
        return config

    @pytest.fixture
    def processing_service(
        self,
        mock_llm_client: MagicMock,
        app_config: AppConfig,
    ) -> ProcessingService:
        """Create a ProcessingService for testing."""
        service_config = ProcessingServiceConfig(
            id="test_profile",
            prompts={"system": "Test system prompt"},
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
        )

        service = ProcessingService(
            llm_client=mock_llm_client,
            tools_provider=AsyncMock(),
            service_config=service_config,
            context_providers=[],
            server_url=None,
            app_config=app_config,
        )
        return service

    def _create_attachment_metadata(
        self, attachment_id: str, description: str = ""
    ) -> AttachmentMetadata:
        """Helper to create attachment metadata."""
        return AttachmentMetadata(
            attachment_id=attachment_id,
            source_type="tool",
            source_id="test_tool",
            mime_type="image/jpeg",
            description=description or f"Test attachment {attachment_id}",
            size=1024,
            created_at=datetime.now(),
        )

    @pytest.mark.asyncio
    async def test_select_attachments_returns_llm_selection(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that _select_attachments_for_response extracts attachment IDs from LLM tool call."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3", "att4", "att5"]

        # Mock registry to return metadata for all attachments
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # Mock LLM response with tool call containing selected IDs
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments={"attachment_ids": ["att2", "att4", "att5"]},
                        ),
                    )
                ],
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Show me the most relevant images",
        )

        assert result == ["att2", "att4", "att5"]
        assert mock_llm_client.generate_response.called

    @pytest.mark.asyncio
    async def test_select_attachments_with_json_string_arguments(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that _select_attachments_for_response handles JSON string arguments from LLM."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM returns arguments as JSON string (common with some LLM providers)
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments=json.dumps({"attachment_ids": ["att1", "att3"]}),
                        ),
                    )
                ],
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        assert result == ["att1", "att3"]

    @pytest.mark.asyncio
    async def test_select_attachments_fallback_on_no_tool_call(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that fallback occurs when LLM doesn't return a tool call."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3", "att4"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM returns no tool calls (just content)
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="I would select the most relevant attachments",
                tool_calls=None,
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should fall back to first N attachments where N = max_response_attachments
        assert len(result) == 4  # max_response_attachments is 6, so all 4 are returned
        assert result == ["att1", "att2", "att3", "att4"]

    @pytest.mark.asyncio
    async def test_select_attachments_fallback_on_wrong_tool_name(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that fallback occurs when LLM calls wrong tool name."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3", "att4"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM calls different tool
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="wrong_tool_name",
                            arguments={"attachment_ids": ["att1"]},
                        ),
                    )
                ],
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should fall back to first N
        assert len(result) == 4
        assert result == ["att1", "att2", "att3", "att4"]

    @pytest.mark.asyncio
    async def test_select_attachments_respects_max_limit(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that selected attachments are truncated to max_response_attachments."""
        processing_service.attachment_registry = mock_attachment_registry

        # max_response_attachments is 6, so we provide more than that
        pending_ids = [f"att{i}" for i in range(1, 11)]  # att1 through att10

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM selects all 10, but should be truncated to max (6)
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments={
                                "attachment_ids": pending_ids  # All 10
                            },
                        ),
                    )
                ],
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should be limited to max_response_attachments (6)
        assert len(result) == 6
        assert result == [f"att{i}" for i in range(1, 7)]

    @pytest.mark.asyncio
    async def test_select_attachments_fallback_respects_max_limit(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that fallback also respects max_response_attachments limit."""
        processing_service.attachment_registry = mock_attachment_registry

        # Provide more than max
        pending_ids = [f"att{i}" for i in range(1, 11)]  # att1 through att10

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM returns no tool calls - triggers fallback
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="Unable to select",
                tool_calls=None,
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should be limited to max (6) even in fallback
        assert len(result) == 6
        assert result == [f"att{i}" for i in range(1, 7)]

    @pytest.mark.asyncio
    async def test_select_attachments_graceful_error_handling(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that errors during selection gracefully fall back to original list."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3"]

        # Mock registry to raise exception
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=RuntimeError("Registry error")
        )

        # LLM doesn't matter since registry fails first
        mock_llm_client.generate_response = AsyncMock()

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should gracefully return original list truncated to max
        assert len(result) == 3
        assert result == ["att1", "att2", "att3"]

    @pytest.mark.asyncio
    async def test_select_attachments_llm_error_handling(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that errors from LLM are handled gracefully."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3", "att4", "att5"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM raises exception
        mock_llm_client.generate_response = AsyncMock(
            side_effect=RuntimeError("LLM error")
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should gracefully return original list truncated to max
        assert len(result) == 5
        assert result == pending_ids

    @pytest.mark.asyncio
    async def test_select_attachments_empty_input(
        self,
        processing_service: ProcessingService,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that empty input list is handled gracefully."""
        processing_service.attachment_registry = mock_attachment_registry

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=[],
            original_query="Select images",
        )

        # Should return empty list
        assert result == []

    @pytest.mark.asyncio
    async def test_select_attachments_no_registry(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
    ) -> None:
        """Test that method handles missing attachment registry gracefully."""
        # Don't set attachment_registry
        processing_service.attachment_registry = None

        pending_ids = ["att1", "att2", "att3"]

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should return original list when no registry
        assert result == pending_ids
        # LLM should not be called
        assert not mock_llm_client.generate_response.called

    @pytest.mark.asyncio
    async def test_select_attachments_malformed_arguments(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test handling of malformed tool arguments from LLM."""
        processing_service.attachment_registry = mock_attachment_registry

        pending_ids = ["att1", "att2", "att3", "att4"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id: self._create_attachment_metadata(att_id)
        )

        # LLM returns malformed arguments (not a dict, not a list)
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments="invalid_arguments",  # String, not dict
                        ),
                    )
                ],
            )
        )

        result = await processing_service._select_attachments_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
        )

        # Should fall back gracefully
        assert len(result) == 4
        assert result == pending_ids
