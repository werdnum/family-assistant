"""
Integration tests for processing.py multimodal tool results handling.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from family_assistant.llm import LLMStreamEvent
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentMetadata
from family_assistant.tools.types import ToolAttachment, ToolResult, ToolReturnType


class TestProcessingServiceMultimodal:
    """Test ProcessingService handling of multimodal tool results"""

    @pytest.fixture
    def mock_tools_provider(self) -> AsyncMock:
        """Create a mock tools provider"""
        provider = AsyncMock()
        return provider

    @pytest.fixture
    def mock_llm_client(self) -> Mock:
        """Create a mock LLM client"""
        client = Mock()
        return client

    @pytest.fixture
    def mock_db_context(self) -> Mock:
        """Create a mock database context"""
        db = Mock()
        return db

    @pytest.fixture
    def processing_service(
        self, mock_llm_client: Mock, mock_tools_provider: AsyncMock
    ) -> ProcessingService:
        """Create a ProcessingService for testing"""

        config = ProcessingServiceConfig(
            id="test_profile",
            prompts={"system": "Test prompt"},
            timezone_str="UTC",
            max_history_messages=10,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="unrestricted",
        )

        service = ProcessingService(
            llm_client=mock_llm_client,
            tools_provider=mock_tools_provider,
            service_config=config,
            context_providers=[],
            server_url=None,
            app_config={},
        )
        return service

    @pytest.mark.asyncio
    async def test_execute_single_tool_string_result(
        self,
        processing_service: ProcessingService,
        mock_tools_provider: AsyncMock,
        mock_db_context: Mock,
    ) -> None:
        """Test _execute_single_tool with traditional string result"""
        # Mock tool call object
        tool_call = Mock()
        tool_call.id = "test_call_123"
        tool_call.function.name = "test_tool"
        tool_call.function.arguments = '{"arg1": "value1"}'

        # Mock tools provider to return string
        mock_tools_provider.execute_tool.return_value = "Simple string result"

        result = await processing_service._execute_single_tool(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_123",
            user_name="test_user",
            turn_id="turn_123",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Should return ToolExecutionResult dataclass
        event = result.stream_event
        tool_message = result.llm_message
        history_message = result.history_message

        # Check event
        assert isinstance(event, LLMStreamEvent)
        assert event.type == "tool_result"
        assert event.tool_call_id == "test_call_123"
        assert event.tool_result == "Simple string result"

        # Check tool message
        assert tool_message.role == "tool"
        assert tool_message.tool_call_id == "test_call_123"
        assert tool_message.content == "Simple string result"
        assert tool_message.error_traceback is None
        assert tool_message.transient_attachments is None

        # Check history message
        assert history_message["role"] == "tool"  # type: ignore[reportIndexIssue]
        assert history_message["tool_call_id"] == "test_call_123"  # type: ignore[reportIndexIssue]
        assert history_message["content"] == "Simple string result"  # type: ignore[reportIndexIssue]

    @pytest.mark.asyncio
    async def test_execute_single_tool_result_with_attachment(
        self,
        processing_service: ProcessingService,
        mock_tools_provider: AsyncMock,
        mock_db_context: Mock,
    ) -> None:
        """Test _execute_single_tool with ToolResult containing attachment"""
        # Mock tool call object
        tool_call = Mock()
        tool_call.id = "test_call_456"
        tool_call.function.name = "image_generator"
        tool_call.function.arguments = '{"prompt": "sunset"}'

        # Create ToolResult with attachment
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake png data",
            description="Generated sunset image",
        )
        tool_result = ToolResult(
            text="Successfully generated sunset image", attachments=[attachment]
        )

        # Mock tools provider to return ToolResult
        mock_tools_provider.execute_tool.return_value = tool_result

        result = await processing_service._execute_single_tool(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_456",
            user_name="test_user",
            turn_id="turn_456",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message
        history_message = result.history_message

        # Check event
        assert isinstance(event, LLMStreamEvent)
        assert event.type == "tool_result"
        assert event.tool_call_id == "test_call_456"
        assert event.tool_result == "Successfully generated sunset image"

        # Check tool message (should have _attachments for provider processing)
        assert tool_message.role == "tool"
        assert tool_message.tool_call_id == "test_call_456"
        assert tool_message.content == "Successfully generated sunset image"
        assert tool_message.error_traceback is None
        assert tool_message.transient_attachments is not None
        assert tool_message.transient_attachments == [attachment]

        # Should also have attachments metadata for history
        assert tool_message.attachments is not None
        assert len(tool_message.attachments) == 1
        attachment_meta = tool_message.attachments[0]
        assert attachment_meta["type"] == "tool_result"
        assert attachment_meta["mime_type"] == "image/png"
        assert attachment_meta["description"] == "Generated sunset image"

        # Check history message (should NOT have transient_attachments but should have metadata)
        assert history_message["role"] == "tool"  # type: ignore[reportIndexIssue]
        assert history_message["tool_call_id"] == "test_call_456"  # type: ignore[reportIndexIssue]
        assert history_message["content"] == "Successfully generated sunset image"  # type: ignore[reportIndexIssue]
        # transient_attachments is excluded from serialization, so it shouldn't be in the dict
        assert "transient_attachments" not in history_message
        assert history_message["attachments"] is not None  # type: ignore[reportIndexIssue]

    @pytest.mark.asyncio
    async def test_execute_single_tool_result_without_attachment(
        self,
        processing_service: ProcessingService,
        mock_tools_provider: AsyncMock,
        mock_db_context: Mock,
    ) -> None:
        """Test _execute_single_tool with ToolResult without attachment"""
        tool_call = Mock()
        tool_call.id = "test_call_789"
        tool_call.function.name = "text_processor"
        tool_call.function.arguments = '{"text": "hello"}'

        # Create ToolResult without attachment
        tool_result = ToolResult(text="Text processed successfully")

        mock_tools_provider.execute_tool.return_value = tool_result

        result = await processing_service._execute_single_tool(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_789",
            user_name="test_user",
            turn_id="turn_789",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message
        history_message = result.history_message

        # Should behave similar to string result when no attachment
        assert event.tool_result == "Text processed successfully"
        assert tool_message.content == "Text processed successfully"
        assert tool_message.transient_attachments is None
        assert tool_message.attachments is None
        assert history_message["content"] == "Text processed successfully"  # type: ignore[reportIndexIssue]

    @pytest.mark.asyncio
    async def test_execute_single_tool_invalid_json_args(
        self,
        processing_service: ProcessingService,
        mock_tools_provider: AsyncMock,
        mock_db_context: Mock,
    ) -> None:
        """Test _execute_single_tool with invalid JSON arguments"""
        tool_call = Mock()
        tool_call.id = "test_call_error"
        tool_call.function.name = "test_tool"
        tool_call.function.arguments = "{invalid json"  # Malformed JSON

        result = await processing_service._execute_single_tool(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_error",
            user_name="test_user",
            turn_id="turn_error",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        # Should return error
        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "error" in event.tool_result.lower()
        assert "invalid arguments" in tool_message.content.lower()
        assert tool_message.error_traceback is not None

    @pytest.mark.asyncio
    async def test_execute_single_tool_execution_error(
        self,
        processing_service: ProcessingService,
        mock_tools_provider: AsyncMock,
        mock_db_context: Mock,
    ) -> None:
        """Test _execute_single_tool when tool execution fails"""
        tool_call = Mock()
        tool_call.id = "test_call_exception"
        tool_call.function.name = "failing_tool"
        tool_call.function.arguments = '{"arg": "value"}'

        # Mock tools provider to raise exception
        mock_tools_provider.execute_tool.side_effect = Exception(
            "Tool execution failed"
        )

        result = await processing_service._execute_single_tool(
            tool_call_item_obj=tool_call,
            interface_type="test",
            conversation_id="conv_exception",
            user_name="test_user",
            turn_id="turn_exception",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        event = result.stream_event
        tool_message = result.llm_message

        # Should handle error gracefully
        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "error" in event.tool_result.lower()
        assert "tool execution failed" in tool_message.content.lower()
        assert tool_message.error_traceback is not None

    def test_tool_result_type_alias(self) -> None:
        """Test ToolReturnType alias works correctly"""

        # Should accept string
        string_result: ToolReturnType = "Simple result"
        assert isinstance(string_result, str)

        # Should accept ToolResult
        tool_result: ToolReturnType = ToolResult(text="Enhanced result")
        assert isinstance(tool_result, ToolResult)

        # Type checking should work (this is mainly for static analysis)
        def mock_tool_function() -> ToolReturnType:
            return "test"

        result = mock_tool_function()
        assert result == "test"

    async def test_automatic_attachment_queuing(
        self, processing_service: ProcessingService, mock_db_context: Mock
    ) -> None:
        """Test that tool result attachments are automatically queued for display"""
        # Mock tool call object with attachment result
        tool_call = Mock()
        tool_call.id = "test_call_auto"
        tool_call.function.name = "mock_camera_snapshot"
        tool_call.function.arguments = '{"entity_id": "camera.test"}'

        # Create ToolResult with attachment
        attachment = ToolAttachment(
            mime_type="image/jpeg",
            content=b"fake jpeg data",
            description="Test camera image",
        )
        tool_result = ToolResult(text="Captured camera image", attachments=[attachment])

        # Mock the attachment registry
        mock_attachment_registry = Mock()

        # Mock store_and_register_tool_attachment (new public method) - returns AttachmentMetadata
        mock_attachment_registry.store_and_register_tool_attachment = AsyncMock(
            return_value=AttachmentMetadata(
                attachment_id="auto_attachment_123",
                source_type="tool",
                source_id="mock_camera_snapshot",
                mime_type="image/jpeg",
                description="Test camera image",
                size=15,
                content_url="http://localhost:8000/attachments/auto_attachment_123",
                storage_path="/tmp/auto_attachment_123.jpeg",
            )
        )

        processing_service.attachment_registry = mock_attachment_registry

        # Mock tools provider to return ToolResult with attachment (async)
        mock_tools_provider = AsyncMock()
        mock_tools_provider.execute_tool.return_value = tool_result
        processing_service.tools_provider = mock_tools_provider

        # Execute single tool
        result = await processing_service._execute_single_tool(
            tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Verify that the attachment ID is returned for auto-queuing
        assert result.auto_attachment_ids == ["auto_attachment_123"]

        # Verify attachment was stored and registered
        mock_attachment_registry.store_and_register_tool_attachment.assert_called_once()

        # Check that store_and_register_tool_attachment was called with correct content
        call_args = (
            mock_attachment_registry.store_and_register_tool_attachment.call_args
        )
        assert call_args[1]["file_content"] == b"fake jpeg data"
        assert call_args[1]["content_type"] == "image/jpeg"
        assert call_args[1]["tool_name"] == "mock_camera_snapshot"

        # Verify that the attachment ID is injected into the LLM message content
        llm_message = result.llm_message
        assert "[Attachment ID(s): auto_attachment_123]" in llm_message.content

        # Verify that the ToolAttachment object has the attachment_id populated
        assert (
            llm_message.transient_attachments is not None
            and llm_message.transient_attachments[0].attachment_id
            == "auto_attachment_123"
        )

    async def test_no_auto_attachment_for_string_results(
        self, processing_service: ProcessingService, mock_db_context: Mock
    ) -> None:
        """Test that string tool results don't generate auto-attachment IDs"""
        # Mock tool call object
        tool_call = Mock()
        tool_call.id = "test_call_string"
        tool_call.function.name = "simple_tool"
        tool_call.function.arguments = '{"text": "hello"}'

        # Mock tools provider to return simple string (async)
        mock_tools_provider = AsyncMock()
        mock_tools_provider.execute_tool.return_value = "Simple text result"
        processing_service.tools_provider = mock_tools_provider

        # Execute single tool
        result = await processing_service._execute_single_tool(
            tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Verify no attachment ID is returned
        assert not result.auto_attachment_ids or len(result.auto_attachment_ids) == 0

    async def test_no_auto_attachment_without_attachment_registry(
        self, processing_service: ProcessingService, mock_db_context: Mock
    ) -> None:
        """Test that ToolResult attachments don't auto-queue without attachment registry"""
        # Mock tool call object
        tool_call = Mock()
        tool_call.id = "test_call_no_service"
        tool_call.function.name = "image_tool"
        tool_call.function.arguments = "{}"

        # Create ToolResult with attachment
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake png data",
            description="Test image",
        )
        tool_result = ToolResult(text="Generated image", attachments=[attachment])

        # No attachment registry configured
        processing_service.attachment_registry = None

        # Mock tools provider (async)
        mock_tools_provider = AsyncMock()
        mock_tools_provider.execute_tool.return_value = tool_result
        processing_service.tools_provider = mock_tools_provider

        # Execute single tool
        result = await processing_service._execute_single_tool(
            tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Should not have auto-attachment ID without attachment registry
        assert not result.auto_attachment_ids or len(result.auto_attachment_ids) == 0

    async def test_attach_to_response_overrides_auto_attachments(
        self, processing_service: ProcessingService, mock_db_context: Mock
    ) -> None:
        """Test that LLM calling attach_to_response replaces auto-queued attachments"""
        # This test simulates the full tool loop behavior where:
        # 1. First tool generates attachment -> auto-queued
        # 2. LLM calls attach_to_response -> replaces auto-queued with explicit list

        # Mock attachment registry and service
        mock_attachment_registry = Mock()
        mock_attachment_registry = Mock()
        processing_service.attachment_registry = mock_attachment_registry

        # Simulate two tool calls in sequence
        # First: image generation tool that auto-queues attachment
        image_tool_call = Mock()
        image_tool_call.id = "call_image_gen"
        image_tool_call.function.name = "generate_image"
        image_tool_call.function.arguments = '{"prompt": "sunset"}'

        # Create attachment for image generation
        attachment = ToolAttachment(
            mime_type="image/png",
            content=b"fake png data",
            description="Generated sunset image",
        )
        image_result = ToolResult(text="Generated image", attachments=[attachment])

        # Mock attachment storage - fix for consolidated AttachmentRegistry
        mock_attachment_registry.store_and_register_tool_attachment = AsyncMock(
            return_value=AttachmentMetadata(
                attachment_id="generated_image_123",
                source_type="tool",
                source_id="generate_image",
                mime_type="image/png",
                description="Generated sunset image",
                size=13,
                content_url="http://localhost:8000/attachments/generated_image_123",
                storage_path="/tmp/generated_image_123.png",
            )
        )

        # Mock tools provider to return different results based on tool name
        mock_tools_provider = AsyncMock()

        def mock_execute_tool(
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            name: str,
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            args: dict[str, Any],
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            context: dict[str, Any],
            call_id: str,
        ) -> ToolReturnType:
            if name == "generate_image":
                return image_result
            elif name == "attach_to_response":
                # Return JSON indicating explicit attachment control
                return '{"status": "attachments_queued", "attachment_ids": ["explicit_attachment_456"], "count": 1, "message": "Explicitly controlling attachments"}'
            return "Unknown tool"

        mock_tools_provider.execute_tool.side_effect = mock_execute_tool
        processing_service.tools_provider = mock_tools_provider

        # Execute first tool (image generation) - should auto-queue
        first_result = await processing_service._execute_single_tool(
            image_tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Verify auto-attachment ID is captured
        assert first_result.auto_attachment_ids == ["generated_image_123"]

        # Second: attach_to_response tool call
        attach_tool_call = Mock()
        attach_tool_call.id = "call_attach_response"
        attach_tool_call.function.name = "attach_to_response"
        attach_tool_call.function.arguments = (
            '{"attachment_ids": ["explicit_attachment_456"]}'
        )

        second_result = await processing_service._execute_single_tool(
            attach_tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # attach_to_response doesn't generate auto-attachments
        assert (
            not second_result.auto_attachment_ids
            or len(second_result.auto_attachment_ids) == 0
        )
        # But it should return the JSON response for the processing loop to handle
        assert second_result.stream_event.tool_result is not None
        assert "attachments_queued" in second_result.stream_event.tool_result

    async def test_multiple_attach_to_response_calls_behavior(
        self, processing_service: ProcessingService, mock_db_context: Mock
    ) -> None:
        """Test behavior when attach_to_response is called multiple times"""
        # This tests the edge case where LLM calls attach_to_response multiple times
        # The last call should win (replace previous explicit attachments)

        mock_tools_provider = AsyncMock()

        # Mock attach_to_response to return different attachment lists
        call_count = 0

        def mock_attach_response(
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            name: str,
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            args: dict[str, Any],
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            context: dict[str, Any],
            call_id: str,
        ) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call with one attachment
                return '{"status": "attachments_queued", "attachment_ids": ["first_attachment"], "count": 1, "message": "First attach call"}'
            else:
                # Second call with different attachments
                return '{"status": "attachments_queued", "attachment_ids": ["second_attachment_a", "second_attachment_b"], "count": 2, "message": "Second attach call"}'

        mock_tools_provider.execute_tool.side_effect = mock_attach_response
        processing_service.tools_provider = mock_tools_provider

        # First attach_to_response call
        first_call = Mock()
        first_call.id = "call_attach_1"
        first_call.function.name = "attach_to_response"
        first_call.function.arguments = '{"attachment_ids": ["first_attachment"]}'

        first_result = await processing_service._execute_single_tool(
            first_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Second attach_to_response call
        second_call = Mock()
        second_call.id = "call_attach_2"
        second_call.function.name = "attach_to_response"
        second_call.function.arguments = (
            '{"attachment_ids": ["second_attachment_a", "second_attachment_b"]}'
        )

        second_result = await processing_service._execute_single_tool(
            second_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Both should return proper JSON responses
        assert first_result.stream_event.tool_result is not None
        assert "first_attachment" in first_result.stream_event.tool_result
        assert second_result.stream_event.tool_result is not None
        assert "second_attachment_a" in second_result.stream_event.tool_result
        assert "second_attachment_b" in second_result.stream_event.tool_result

        # Neither generates auto-attachments (attach_to_response is explicit control)
        assert (
            not first_result.auto_attachment_ids
            or len(first_result.auto_attachment_ids) == 0
        )
        assert (
            not second_result.auto_attachment_ids
            or len(second_result.auto_attachment_ids) == 0
        )

    async def test_new_attachments_after_attach_to_response(
        self, processing_service: ProcessingService, mock_db_context: Mock
    ) -> None:
        """Test behavior when new tool attachments appear after attach_to_response is called"""
        # This tests the scenario:
        # 1. LLM calls attach_to_response (explicit control)
        # 2. Later tool generates new attachment
        # Question: Should the new attachment be auto-queued or ignored?
        # Answer: It should be auto-queued! Each attachment is independent.

        # Mock attachment registry for this test
        mock_attachment_registry = Mock()

        # Mock store_and_register_tool_attachment for the new tool attachment
        mock_attachment_registry.store_and_register_tool_attachment = AsyncMock(
            return_value=AttachmentMetadata(
                attachment_id="new_attachment_after_explicit",
                source_type="tool",
                source_id="generate_new_image",
                mime_type="image/jpeg",
                description="New generated image",
                size=13,
                content_url="http://localhost:8000/attachments/new_attachment_after_explicit",
                storage_path="/tmp/new_attachment_after_explicit.jpeg",
            )
        )

        # Mock get_attachment for the attach_to_response tool
        mock_attachment_registry.get_attachment = AsyncMock(
            return_value=None
        )  # Return None for explicit_attachment lookup

        processing_service.attachment_registry = mock_attachment_registry

        mock_tools_provider = AsyncMock()

        def mock_execute_tool(
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            name: str,
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            args: dict[str, Any],
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            context: dict[str, Any],
            call_id: str,
        ) -> ToolReturnType:
            if name == "attach_to_response":
                return '{"status": "attachments_queued", "attachment_ids": ["explicit_attachment"], "count": 1, "message": "Explicit control established"}'
            elif name == "generate_new_image":
                # New tool that generates attachment after explicit control was established
                attachment = ToolAttachment(
                    mime_type="image/jpeg",
                    content=b"new image data",
                    description="New generated image",
                )
                return ToolResult(text="Generated new image", attachments=[attachment])
            return "Unknown tool"

        mock_tools_provider.execute_tool.side_effect = mock_execute_tool
        processing_service.tools_provider = mock_tools_provider

        # First: LLM calls attach_to_response (establishes explicit control)
        attach_call = Mock()
        attach_call.id = "call_explicit"
        attach_call.function.name = "attach_to_response"
        attach_call.function.arguments = '{"attachment_ids": ["explicit_attachment"]}'

        attach_result = await processing_service._execute_single_tool(
            attach_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Second: New tool generates attachment
        new_tool_call = Mock()
        new_tool_call.id = "call_new_image"
        new_tool_call.function.name = "generate_new_image"
        new_tool_call.function.arguments = "{}"

        new_tool_result = await processing_service._execute_single_tool(
            new_tool_call,
            interface_type="test",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=mock_db_context,
            chat_interface=None,
            request_confirmation_callback=None,
        )

        # Attach call should not generate auto-attachment
        assert (
            not attach_result.auto_attachment_ids
            or len(attach_result.auto_attachment_ids) == 0
        )

        # But new tool should still auto-queue its attachment
        # (Each tool execution is independent)
        assert new_tool_result.auto_attachment_ids == ["new_attachment_after_explicit"]

        # The processing loop will handle the logic of:
        # - Auto-queue from new tool
        # - But if there's another attach_to_response later, it replaces everything
