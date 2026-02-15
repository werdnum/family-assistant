"""Integration tests for the Gemini Computer Use profile."""

import logging
import os
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.storage.context import get_db_context
from family_assistant.tools.computer_use import (
    BrowserSession,
    close_browser_session,
    computer_use_click_at,
    computer_use_navigate,
    get_browser_session,
)
from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


@pytest.fixture
async def gemini_client() -> AsyncGenerator[GoogleGenAIClient]:
    """Create a GoogleGenAIClient instance for testing."""
    api_key = os.getenv("GEMINI_API_KEY", "dummy_key")
    # Use the computer use model to trigger the specific logic
    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.5-computer-use-preview-10-2025",
        enable_url_context=False,
        enable_google_search=False,
    )
    yield client
    await client.close()


@pytest.fixture
def mock_exec_context() -> ToolExecutionContext:
    """Create a mock ToolExecutionContext for testing."""
    return MagicMock(spec=ToolExecutionContext, conversation_id="test-conversation")


@pytest.fixture
async def browser_session(
    mock_exec_context: ToolExecutionContext,
) -> AsyncGenerator[BrowserSession]:
    """Create and cleanup a browser session for testing."""
    session = await get_browser_session(mock_exec_context)
    yield session
    await close_browser_session(mock_exec_context)


@pytest.mark.asyncio
async def test_computer_use_tool_injection(gemini_client: GoogleGenAIClient) -> None:
    """Test that the Computer Use tool is automatically injected for the correct model."""

    # Verify the model detection logic
    assert gemini_client._is_computer_use_model(
        "gemini-2.5-computer-use-preview-10-2025"
    )
    assert not gemini_client._is_computer_use_model("gemini-1.5-pro")

    # Mock the underlying SDK client generate_content method
    # We want to verify that `tools` in the config contains the ComputerUse tool
    with patch.object(
        gemini_client.client.aio.models, "generate_content"
    ) as mock_generate:
        # Mock response to avoid actual API call failure if key is dummy
        part_mock = MagicMock(text="Response")
        # Ensure thought_signature is None so it doesn't trigger processing
        part_mock.thought_signature = None
        part_mock.function_call = None

        mock_response = MagicMock()
        mock_response.candidates = [MagicMock(content=MagicMock(parts=[part_mock]))]
        mock_generate.return_value = mock_response

        # Call generate_response with some dummy messages
        messages = [UserMessage(content="Navigate to google.com")]

        # We pass some dummy tools to verify filtering logic too
        dummy_tools: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "click_at",  # Should be filtered out
                    "description": "Manual click_at",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "other_tool",  # Should be kept
                    "description": "Some other tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

        await gemini_client.generate_response(messages, tools=dummy_tools)

        # Verify arguments passed to generate_content
        call_args = mock_generate.call_args
        assert call_args is not None
        _, kwargs = call_args

        config = kwargs.get("config")
        assert config is not None
        assert isinstance(config, types.GenerateContentConfig)

        # Check tools
        tools_passed = config.tools
        assert tools_passed is not None

        # Verify ComputerUse tool is present
        has_computer_use = False
        for tool in tools_passed:
            # Use isinstance for proper type narrowing
            if isinstance(tool, types.Tool) and tool.computer_use:
                has_computer_use = True
                assert (
                    tool.computer_use.environment
                    == types.Environment.ENVIRONMENT_BROWSER
                )
                break

        assert has_computer_use, (
            f"Computer Use tool was not injected. Tools: {tools_passed}"
        )

        # Verify manual 'click_at' was filtered out, but 'other_tool' remains
        has_click_at = False
        has_other_tool = False

        for tool in tools_passed:
            # Use isinstance for proper type narrowing
            if isinstance(tool, types.Tool) and tool.function_declarations:
                for func in tool.function_declarations:
                    if func.name == "click_at":
                        has_click_at = True
                    if func.name == "other_tool":
                        has_other_tool = True

        assert not has_click_at, (
            "Manual 'click_at' tool definition should be filtered out"
        )
        assert has_other_tool, "'other_tool' should be preserved"


@pytest.mark.asyncio
async def test_computer_use_end_to_end_flow(gemini_client: GoogleGenAIClient) -> None:
    """Test the end-to-end flow of tool calling and response handling with Computer Use."""
    # This test verifies that the client correctly processes a Computer Use function call
    # and prepares the next request.

    # 1. Mock the model returning a 'click_at' call
    with patch.object(
        gemini_client.client.aio.models, "generate_content"
    ) as mock_generate:
        # Construct a response with a function call
        mock_response = MagicMock()

        # Mock Part with function_call
        function_call_part = MagicMock()
        function_call_part.text = None
        function_call_part.function_call = MagicMock()
        function_call_part.function_call.name = "click_at"
        function_call_part.function_call.args = {"x": 500, "y": 300}
        function_call_part.function_call.id = "call_123"
        function_call_part.thought_signature = None  # Optional thought signature

        # Mock Candidate
        candidate = MagicMock()
        candidate.content.parts = [function_call_part]
        mock_response.candidates = [candidate]
        mock_response.usage_metadata = MagicMock()

        mock_generate.return_value = mock_response

        # Execute
        messages = [UserMessage(content="Click the button")]
        response = await gemini_client.generate_response(messages)

        # Verify we got a tool call
        assert response.tool_calls is not None
        assert len(response.tool_calls) == 1
        tool_call = response.tool_calls[0]
        assert tool_call.function.name == "click_at"
        assert tool_call.function.arguments == {"x": 500, "y": 300}


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("GEMINI_API_KEY") is None, reason="Requires GEMINI_API_KEY"
)
@pytest.mark.asyncio
async def test_real_gemini_computer_use_protocol() -> None:
    """
    Test against the real Gemini API to verify the protocol works.

    This test sends a request to the real model (if key is present) and verifies
    it doesn't crash when configured with Computer Use.
    """
    api_key = os.environ["GEMINI_API_KEY"]
    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.5-computer-use-preview-10-2025",
    )

    try:
        # We don't need to actually execute a browser action here (that's heavy),
        # but we want to verify the model accepts our tool configuration.
        # We'll ask it something that *might* trigger a tool call or at least a text response.
        messages = [UserMessage(content="What is the current time?")]

        # We pass no extra tools, relying on the auto-injection
        response = await client.generate_response(messages)

        # We just want to ensure we got a valid response (text or tool call)
        # and no API error about invalid tool configuration.
        assert response.content is not None or response.tool_calls is not None

    finally:
        await client.close()


# --- Integration tests that test the actual tool code ---


class TestComputerUseTools:
    """Integration tests for Computer Use tool functions with real Playwright."""

    @pytest.mark.asyncio
    async def test_browser_session_lifecycle(
        self, mock_exec_context: ToolExecutionContext
    ) -> None:
        """Test that browser sessions are properly created and cleaned up."""
        # Get a session
        session1 = await get_browser_session(mock_exec_context)
        assert session1 is not None
        assert session1.page is None  # Not initialized until ensure_page()

        # Same context should return same session
        session2 = await get_browser_session(mock_exec_context)
        assert session1 is session2

        # Ensure page creates browser resources
        page = await session1.ensure_page()
        assert page is not None
        assert session1.browser is not None
        assert session1.context is not None
        assert session1.playwright is not None

        # Cleanup
        await close_browser_session(mock_exec_context)

        # Session should be removed
        session3 = await get_browser_session(mock_exec_context)
        assert session3 is not session1

        # Cleanup the new session
        await close_browser_session(mock_exec_context)

    @pytest.mark.asyncio
    async def test_navigate_tool(
        self, mock_exec_context: ToolExecutionContext, browser_session: BrowserSession
    ) -> None:
        """Test navigation to a URL using the actual tool function."""
        # Navigate to example.com
        result = await computer_use_navigate(mock_exec_context, "https://example.com")

        # Verify we got a ToolResult with URL and screenshot
        assert result is not None
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert "url" in result.data
        assert "example.com" in result.data["url"]

        # Verify we got a screenshot attachment
        assert result.attachments is not None
        assert len(result.attachments) == 1
        attachment = result.attachments[0]
        assert attachment.mime_type == "image/png"
        assert attachment.content is not None
        assert len(attachment.content) > 0

        # Verify we're on the right page
        page = await browser_session.ensure_page()
        assert "example.com" in page.url

    @pytest.mark.asyncio
    async def test_navigate_adds_protocol(
        self, mock_exec_context: ToolExecutionContext, browser_session: BrowserSession
    ) -> None:
        """Test that navigate adds https:// if protocol is missing."""
        # Navigate without protocol
        result = await computer_use_navigate(mock_exec_context, "example.com")

        assert result is not None
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert result.data["url"].startswith("https://")

        # Verify the URL has https
        page = await browser_session.ensure_page()
        assert page.url.startswith("https://")

    @pytest.mark.asyncio
    async def test_click_at_tool(
        self, mock_exec_context: ToolExecutionContext, browser_session: BrowserSession
    ) -> None:
        """Test clicking at coordinates using the actual tool function."""
        # First navigate to a page
        await computer_use_navigate(mock_exec_context, "https://example.com")

        # Click at the center of the screen (normalized coordinates)
        result = await computer_use_click_at(mock_exec_context, x=500, y=500)

        # Verify we got a ToolResult with URL and screenshot
        assert result is not None
        assert result.data is not None
        assert isinstance(result.data, dict)
        assert "url" in result.data

        # Verify we got a screenshot attachment
        assert result.attachments is not None
        assert len(result.attachments) == 1
        attachment = result.attachments[0]
        assert attachment.mime_type == "image/png"
        assert attachment.content is not None
        assert len(attachment.content) > 0


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("GEMINI_API_KEY") is None, reason="Requires GEMINI_API_KEY"
)
@pytest.mark.asyncio
async def test_computer_use_browser_navigation_e2e(db_engine: AsyncEngine) -> None:
    """
    End-to-end integration test of browser automation with Gemini Computer Use.

    This test uses the full Assistant/ProcessingService stack to verify:
    1. LLM receives a task and decides what browser actions to take
    2. ProcessingService executes tools through LocalToolsProvider
    3. Tool results (screenshots) are sent back to the LLM
    4. Loop continues until task completion

    The task: Navigate to example.com and report the page heading.
    """
    api_key = os.environ["GEMINI_API_KEY"]

    # Create a browser profile configuration for testing
    test_profile_id = "browser_test_profile"
    # ast-grep-ignore: no-dict-any - Test configuration for AppConfig
    test_config: dict[str, Any] = {
        "gemini_api_key": api_key,
        "database_url": str(db_engine.url),
        "default_service_profile_id": test_profile_id,
        "service_profiles": [
            {
                "id": test_profile_id,
                "description": "Browser test profile with computer use",
                "processing_config": {
                    "provider": "google",
                    "llm_model": "gemini-2.5-computer-use-preview-10-2025",
                    "max_iterations": 15,
                    "prompts": {
                        "system_prompt": (
                            "You are a browser automation assistant. "
                            "Use the browser tools to navigate and interact with web pages."
                        )
                    },
                    "timezone": "UTC",
                    "max_history_messages": 10,
                    "history_max_age_hours": 1,
                    "delegation_security_level": "unrestricted",
                },
                "tools_config": {
                    "enable_local_tools": [
                        "click_at",
                        "type_text_at",
                        "scroll_at",
                        "open_web_browser",
                        "navigate",
                        "search",
                        "go_back",
                        "go_forward",
                        "key_combination",
                        "wait_5_seconds",
                        "hover_at",
                        "drag_and_drop",
                        "scroll_document",
                    ],
                    "enable_mcp_server_ids": [],
                    "confirm_tools": [],
                },
            }
        ],
        "mcp_config": {"mcpServers": {}},
        # Minimal config for other required fields
        "telegram_enabled": False,
        "telegram_token": None,
        "allowed_user_ids": [],
        "developer_chat_id": None,
        "model": "gemini-2.5-computer-use-preview-10-2025",
        "embedding_model": "mock-deterministic-embedder",
        "embedding_dimensions": 10,
        "server_url": "http://localhost:8000",
        "document_storage_path": "/tmp/test_docs",
        "attachment_storage_path": "/tmp/test_attachments",
        "indexing_pipeline_config": {"processors": []},
        "message_batching_config": {"strategy": "none", "delay_seconds": 0},
        "llm_parameters": {},
    }

    # Create Assistant with the test configuration
    assistant = Assistant(
        config=AppConfig.model_validate(test_config),
        database_engine=db_engine,
    )

    try:
        # Setup dependencies (creates ProcessingService, tools_provider, etc.)
        await assistant.setup_dependencies()

        assert assistant.default_processing_service is not None
        processing_service = assistant.default_processing_service

        # Create a database context for the test
        async with get_db_context(engine=db_engine) as db_context:
            # Process the user's request through the full stack
            # The ProcessingService will handle the LLM → tool → result loop
            (
                turn_messages,
                reasoning_info,
                attachment_ids,
            ) = await processing_service.process_message(
                db_context=db_context,
                messages=[
                    UserMessage(
                        content=(
                            "Navigate to https://example.com and tell me what the "
                            "main heading (h1) on the page says. "
                            "Use the browser tools to navigate there."
                        ),
                    )
                ],
                interface_type="test",
                conversation_id="e2e-browser-test",
                user_name="TestUser",
                turn_id="turn-1",
                chat_interface=None,
            )

            # Find the final assistant response
            final_response = None
            for msg in reversed(turn_messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    final_response = msg["content"]
                    break

            logger.info(f"Final response: {final_response}")
            logger.info(f"Total messages in turn: {len(turn_messages)}")

            # Verify the task was completed - response should mention "Example Domain"
            assert final_response is not None, "No final assistant response received"
            assert "Example Domain" in final_response, (
                f"Expected 'Example Domain' in response, got: {final_response}"
            )

    finally:
        # Cleanup: close any browser sessions
        # The conversation_id used by ProcessingService for tool context
        cleanup_context = MagicMock(
            spec=ToolExecutionContext, conversation_id="e2e-browser-test"
        )
        await close_browser_session(cleanup_context)

        # Shutdown assistant
        await assistant.stop_services()


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("GEMINI_API_KEY") is None, reason="Requires GEMINI_API_KEY"
)
@pytest.mark.asyncio
async def test_grab_screenshot_of_website(db_engine: AsyncEngine) -> None:
    """
    Test the 'grab a screenshot of website X' use case.

    This is a core use case for Computer Use: user asks to take a screenshot
    of a website and the system returns the screenshot in attachments.

    Verifies:
    1. User can request a screenshot of a website
    2. ProcessingService correctly executes browser tools
    3. Screenshot is captured and returned as an attachment
    """
    api_key = os.environ["GEMINI_API_KEY"]

    test_profile_id = "screenshot_test_profile"
    # ast-grep-ignore: no-dict-any - Test configuration for AppConfig
    test_config: dict[str, Any] = {
        "gemini_api_key": api_key,
        "database_url": str(db_engine.url),
        "default_service_profile_id": test_profile_id,
        "service_profiles": [
            {
                "id": test_profile_id,
                "description": "Screenshot test profile with computer use",
                "processing_config": {
                    "provider": "google",
                    "llm_model": "gemini-2.5-computer-use-preview-10-2025",
                    "max_iterations": 10,
                    "prompts": {
                        "system_prompt": (
                            "You are a browser automation assistant. "
                            "Use the browser tools to navigate and take screenshots."
                        )
                    },
                    "timezone": "UTC",
                    "max_history_messages": 10,
                    "history_max_age_hours": 1,
                    "delegation_security_level": "unrestricted",
                },
                "tools_config": {
                    "enable_local_tools": [
                        "click_at",
                        "type_text_at",
                        "scroll_at",
                        "open_web_browser",
                        "navigate",
                        "search",
                        "go_back",
                        "go_forward",
                        "key_combination",
                        "wait_5_seconds",
                        "hover_at",
                        "drag_and_drop",
                        "scroll_document",
                    ],
                    "enable_mcp_server_ids": [],
                    "confirm_tools": [],
                },
            }
        ],
        "mcp_config": {"mcpServers": {}},
        "telegram_enabled": False,
        "telegram_token": None,
        "allowed_user_ids": [],
        "developer_chat_id": None,
        "model": "gemini-2.5-computer-use-preview-10-2025",
        "embedding_model": "mock-deterministic-embedder",
        "embedding_dimensions": 10,
        "server_url": "http://localhost:8000",
        "document_storage_path": "/tmp/test_docs",
        "attachment_storage_path": "/tmp/test_attachments",
        "indexing_pipeline_config": {"processors": []},
        "message_batching_config": {"strategy": "none", "delay_seconds": 0},
        "llm_parameters": {},
    }

    assistant = Assistant(
        config=AppConfig.model_validate(test_config),
        database_engine=db_engine,
    )

    try:
        await assistant.setup_dependencies()

        assert assistant.default_processing_service is not None
        processing_service = assistant.default_processing_service

        async with get_db_context(engine=db_engine) as db_context:
            # User asks to take a screenshot of a website
            (
                turn_messages,
                reasoning_info,
                attachment_ids,
            ) = await processing_service.process_message(
                db_context=db_context,
                messages=[
                    UserMessage(
                        content=(
                            "Please take a screenshot of https://example.com. "
                            "Navigate to the website and capture what you see."
                        ),
                    )
                ],
                interface_type="test",
                conversation_id="screenshot-test",
                user_name="TestUser",
                turn_id="turn-1",
                chat_interface=None,
            )

            # Find the final assistant response
            final_response = None
            for msg in reversed(turn_messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    final_response = msg["content"]
                    break

            logger.info(f"Final response: {final_response}")
            logger.info(f"Attachment IDs: {attachment_ids}")
            logger.info(f"Total messages in turn: {len(turn_messages)}")

            # Verify screenshot was taken
            # The attachment_ids list should contain the screenshot attachment
            assert final_response is not None, "No final assistant response received"

            # The response should indicate the screenshot was taken
            # and there should be at least one attachment (the screenshot)
            assert attachment_ids is not None and len(attachment_ids) > 0, (
                f"Expected screenshot attachment(s), got: {attachment_ids}. "
                f"Response: {final_response}"
            )

            logger.info(
                f"Screenshot test passed: {len(attachment_ids)} attachment(s) captured"
            )

    finally:
        cleanup_context = MagicMock(
            spec=ToolExecutionContext, conversation_id="screenshot-test"
        )
        await close_browser_session(cleanup_context)
        await assistant.stop_services()
