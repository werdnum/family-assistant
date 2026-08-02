"""
Unit tests for attachment selection logic in ProcessingService.

Tests the _select_attachments_for_response method and related threshold logic.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.attachments import AttachmentSelectionError
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
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
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
    async def test_attachment_selection_handles_small_candidate_sets(
        self, processing_service: ProcessingService
    ) -> None:
        """Selection can still run correctly with a small candidate set."""
        pending_attachment_ids = ["att1", "att2", "att3"]

        processing_service.attachment_processor.attachment_registry.get_attachment_with_context = AsyncMock(  # type: ignore[union-attr]
            side_effect=lambda att_id, acting_user_id=None: AttachmentMetadata(
                attachment_id=att_id,
                source_type="tool",
                source_id="test_tool",
                mime_type="image/jpeg",
                description=f"Test attachment {att_id}",
                size=1024,
                created_at=datetime.now(UTC),
            )
        )
        processing_service.llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id="call_123",
                        type="function",
                        function=ToolCallFunction(
                            name="attach_to_response",
                            arguments={"attachment_ids": pending_attachment_ids},
                        ),
                    )
                ],
            )
        )

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_attachment_ids,
            original_query="Test query",
            acting_user_id=None,
        )

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

        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        # Create mock attachment metadata
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: AttachmentMetadata(
                attachment_id=att_id,
                source_type="tool",
                source_id="test_tool",
                mime_type="image/jpeg",
                description=f"Test attachment {att_id}",
                size=1024,
                created_at=datetime.now(UTC),
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

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_attachment_ids,
            original_query="Test query",
            acting_user_id=None,
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
            timezone=ZoneInfo("UTC"),
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
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
            created_at=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_select_attachments_returns_llm_selection(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that _select_attachments_for_response extracts attachment IDs from LLM tool call."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3", "att4", "att5"]

        # Mock registry to return metadata for all attachments
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
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

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Show me the most relevant images",
            acting_user_id=None,
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
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
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

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
            acting_user_id=None,
        )

        assert result == ["att1", "att3"]

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_no_tool_call(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Selection fails explicitly when LLM returns no tool call."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3", "att4"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
        )

        # LLM returns no tool calls (just content)
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="I would select the most relevant attachments",
                tool_calls=None,
            )
        )

        with pytest.raises(AttachmentSelectionError):
            await processing_service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_wrong_tool_name(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Selection fails explicitly when LLM calls a wrong tool."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3", "att4"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
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

        with pytest.raises(AttachmentSelectionError):
            await processing_service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_respects_max_limit(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that selected attachments are truncated to max_response_attachments."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        # max_response_attachments is 6, so we provide more than that
        pending_ids = [f"att{i}" for i in range(1, 11)]  # att1 through att10

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
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

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
            acting_user_id=None,
        )

        # Should be limited to max_response_attachments (6)
        assert len(result) == 6
        assert result == [f"att{i}" for i in range(1, 7)]

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_missing_tool_call_even_with_many_attachments(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Selection fails explicitly when tool call is missing."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        # Provide more than max
        pending_ids = [f"att{i}" for i in range(1, 11)]  # att1 through att10

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
        )

        # LLM returns no tool calls - triggers fallback
        mock_llm_client.generate_response = AsyncMock(
            return_value=LLMOutput(
                content="Unable to select",
                tool_calls=None,
            )
        )

        with pytest.raises(AttachmentSelectionError):
            await processing_service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_raises_on_metadata_error(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Unexpected metadata read errors are not silently swallowed."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3"]

        # Mock registry to raise exception
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=RuntimeError("Registry error")
        )

        # LLM doesn't matter since registry fails first
        mock_llm_client.generate_response = AsyncMock()

        with pytest.raises(RuntimeError, match="Registry error"):
            await processing_service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_raises_on_llm_error(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """LLM failures surface as explicit selection errors."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3", "att4", "att5"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
        )

        # LLM raises exception
        mock_llm_client.generate_response = AsyncMock(
            side_effect=RuntimeError("LLM error")
        )

        with pytest.raises(
            AttachmentSelectionError,
            match="Failed to run LLM-based attachment selection",
        ):
            await processing_service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
            )

    @pytest.mark.asyncio
    async def test_select_attachments_empty_input(
        self,
        processing_service: ProcessingService,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test that empty input list is handled gracefully."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=[],
            original_query="Select images",
            acting_user_id=None,
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
        processing_service.attachment_processor.attachment_registry = None

        pending_ids = ["att1", "att2", "att3"]

        result = await processing_service.attachment_processor.select_for_response(
            pending_attachment_ids=pending_ids,
            original_query="Select images",
            acting_user_id=None,
        )

        # Should return original list when no registry
        assert result == pending_ids
        # LLM should not be called
        assert not mock_llm_client.generate_response.called

    @pytest.mark.asyncio
    async def test_select_attachments_errors_on_malformed_arguments(
        self,
        processing_service: ProcessingService,
        mock_llm_client: MagicMock,
        mock_attachment_registry: AsyncMock,
    ) -> None:
        """Test handling of malformed tool arguments from LLM."""
        processing_service.attachment_processor.attachment_registry = (
            mock_attachment_registry
        )

        pending_ids = ["att1", "att2", "att3", "att4"]

        # Mock registry
        mock_attachment_registry.get_attachment_with_context = AsyncMock(
            side_effect=lambda att_id, acting_user_id=None: (
                self._create_attachment_metadata(att_id)
            )
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

        with pytest.raises(AttachmentSelectionError):
            await processing_service.attachment_processor.select_for_response(
                pending_attachment_ids=pending_ids,
                original_query="Select images",
                acting_user_id=None,
            )
