"""
Integration tests for processing.py multimodal tool results handling.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from family_assistant.llm import LLMStreamEvent
from family_assistant.processing import ProcessingService
from family_assistant.tools.types import ToolAttachment, ToolResult


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
        from family_assistant.processing import ProcessingServiceConfig

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

        # Should return tuple of (event, tool_message, history_message)
        event, tool_message, history_message = result

        # Check event
        assert isinstance(event, LLMStreamEvent)
        assert event.type == "tool_result"
        assert event.tool_call_id == "test_call_123"
        assert event.tool_result == "Simple string result"

        # Check tool message
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "test_call_123"
        assert tool_message["content"] == "Simple string result"
        assert tool_message["error_traceback"] is None
        assert "_attachment" not in tool_message

        # Check history message
        assert history_message["role"] == "tool"
        assert history_message["tool_call_id"] == "test_call_123"
        assert history_message["content"] == "Simple string result"

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
            text="Successfully generated sunset image", attachment=attachment
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

        event, tool_message, history_message = result

        # Check event
        assert isinstance(event, LLMStreamEvent)
        assert event.type == "tool_result"
        assert event.tool_call_id == "test_call_456"
        assert event.tool_result == "Successfully generated sunset image"

        # Check tool message (should have _attachment for provider processing)
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "test_call_456"
        assert tool_message["content"] == "Successfully generated sunset image"
        assert tool_message["error_traceback"] is None
        assert "_attachment" in tool_message
        assert tool_message["_attachment"] == attachment

        # Should also have attachments metadata for history
        assert "attachments" in tool_message
        assert len(tool_message["attachments"]) == 1
        attachment_meta = tool_message["attachments"][0]
        assert attachment_meta["type"] == "tool_result"
        assert attachment_meta["mime_type"] == "image/png"
        assert attachment_meta["description"] == "Generated sunset image"

        # Check history message (should NOT have _attachment but should have metadata)
        assert history_message["role"] == "tool"
        assert history_message["tool_call_id"] == "test_call_456"
        assert history_message["content"] == "Successfully generated sunset image"
        assert "_attachment" not in history_message  # Raw data removed
        assert "attachments" in history_message  # Metadata preserved

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

        event, tool_message, history_message = result

        # Should behave similar to string result when no attachment
        assert event.tool_result == "Text processed successfully"
        assert tool_message["content"] == "Text processed successfully"
        assert "_attachment" not in tool_message
        assert "attachments" not in tool_message
        assert history_message["content"] == "Text processed successfully"

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

        event, tool_message, history_message = result

        # Should return error
        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "error" in event.tool_result.lower()
        assert "invalid arguments" in tool_message["content"].lower()
        assert tool_message["error_traceback"] is not None

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

        event, tool_message, history_message = result

        # Should handle error gracefully
        assert event.type == "tool_result"
        assert event.tool_result is not None
        assert "error" in event.tool_result.lower()
        assert "tool execution failed" in tool_message["content"].lower()
        assert tool_message["error_traceback"] is not None

    def test_tool_result_type_alias(self) -> None:
        """Test ToolReturnType alias works correctly"""
        from family_assistant.tools.types import ToolReturnType

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
