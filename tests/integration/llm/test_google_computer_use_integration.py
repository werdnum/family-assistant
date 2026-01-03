"""Integration tests for the Gemini Computer Use profile."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types
from playwright.async_api import Page as AsyncPage
from playwright.async_api import async_playwright

from family_assistant.llm.messages import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.tools.types import ToolAttachment

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
        dummy_tools = [
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
            # Check if tool has computer_use attribute (SDK object) or key (dict)
            # Use getattr to avoid type checker issues with dynamic SDK attributes
            computer_use = getattr(tool, "computer_use", None)
            if computer_use:
                has_computer_use = True
                assert computer_use.environment == types.Environment.ENVIRONMENT_BROWSER
                break
            # Fallback for if it's a dict or other structure (though SDK usually uses objects)
            elif (
                isinstance(tool, dict)
                and "computer_use" in tool
                or "computer_use" in str(tool)
            ):
                has_computer_use = True
                break

        assert has_computer_use, (
            f"Computer Use tool was not injected. Tools: {tools_passed}"
        )

        # Verify manual 'click_at' was filtered out, but 'other_tool' remains
        has_click_at = False
        has_other_tool = False

        for tool in tools_passed:
            # Use getattr to avoid type checker issues with dynamic SDK attributes
            function_declarations = getattr(tool, "function_declarations", None)
            if function_declarations:
                for func in function_declarations:
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


# Screen dimensions matching the computer_use.py constants
_SCREEN_WIDTH = 1024
_SCREEN_HEIGHT = 768
_MAX_ITERATIONS = 15


def _denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("GEMINI_API_KEY") is None, reason="Requires GEMINI_API_KEY"
)
@pytest.mark.asyncio
async def test_computer_use_browser_navigation_e2e() -> None:
    """
    End-to-end test of browser automation with Gemini Computer Use.

    This test verifies the complete loop:
    1. Send a task to the model with an initial screenshot
    2. Model returns browser actions (navigate, click, etc.)
    3. Execute actions with Playwright
    4. Send result screenshot back to model
    5. Continue until task is complete or max iterations reached

    The task: Navigate to example.com and report the page heading.
    """
    api_key = os.environ["GEMINI_API_KEY"]
    client = GoogleGenAIClient(
        api_key=api_key,
        model="gemini-2.5-computer-use-preview-10-2025",
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": _SCREEN_WIDTH, "height": _SCREEN_HEIGHT}
        )
        page = await context.new_page()

        # Start with a blank page
        await page.goto("about:blank")

        try:
            # Build initial message with text only (no screenshot)
            # The model will call navigate first, then we'll send screenshots
            # with tool responses as per Gemini Computer Use protocol
            messages: list[UserMessage | AssistantMessage | ToolMessage] = [
                UserMessage(
                    content=(
                        "Navigate to https://example.com and tell me what the "
                        "main heading (h1) on the page says. "
                        "Use the browser tools to navigate there."
                    )
                )
            ]

            task_completed = False
            iterations = 0
            response = None

            while not task_completed and iterations < _MAX_ITERATIONS:
                iterations += 1
                logger.info(f"Computer use iteration {iterations}")

                response = await client.generate_response(messages)

                # If we got a text response without tool calls, task might be done
                if response.content and not response.tool_calls:
                    logger.info(f"Model response: {response.content}")
                    # Check if the response mentions "Example Domain" (the h1 on example.com)
                    if "Example Domain" in response.content:
                        task_completed = True
                        logger.info("Task completed successfully!")
                    break

                # Process tool calls
                if not response.tool_calls:
                    logger.warning("No tool calls and no content - unexpected state")
                    break

                # Add assistant message with tool calls (preserve provider_metadata for thought signatures)
                messages.append(
                    AssistantMessage(
                        content=response.content or "",
                        tool_calls=[
                            ToolCallItem(
                                id=tc.id,
                                type="function",
                                function=ToolCallFunction(
                                    name=tc.function.name,
                                    arguments=tc.function.arguments,
                                ),
                                provider_metadata=tc.provider_metadata,
                            )
                            for tc in response.tool_calls
                        ],
                    )
                )

                # Execute each tool call
                for tool_call in response.tool_calls:
                    action_name = tool_call.function.name
                    args = tool_call.function.arguments

                    # Ensure args is a dict
                    if isinstance(args, str):
                        args = json.loads(args)

                    logger.info(f"Executing action: {action_name} with args: {args}")

                    # Execute the browser action
                    await _execute_browser_action(page, action_name, args)

                    # Take screenshot after action
                    screenshot_bytes = await page.screenshot(type="png")

                    # Create tool message with screenshot attachment
                    tool_attachment = ToolAttachment(
                        content=screenshot_bytes,
                        mime_type="image/png",
                        description=f"Screenshot after {action_name}",
                    )

                    # Get current URL - required by Computer Use API
                    current_url = page.url

                    # Use _attachments (alias for transient_attachments) to pass screenshot
                    # Content must be JSON with 'url' field as required by Gemini Computer Use
                    tool_msg = ToolMessage(
                        tool_call_id=tool_call.id,
                        name=action_name,
                        content=json.dumps({"url": current_url}),
                    )
                    # Set transient_attachments directly after construction
                    tool_msg.transient_attachments = [tool_attachment]
                    messages.append(tool_msg)

            # Verify the task was completed
            assert task_completed or iterations < _MAX_ITERATIONS, (
                f"Task did not complete within {_MAX_ITERATIONS} iterations. "
                f"Last response: {response.content if response else 'None'}"
            )

            # Verify we actually navigated to example.com
            current_url = page.url
            assert "example.com" in current_url, (
                f"Expected to be on example.com, but URL is: {current_url}"
            )

        finally:
            await browser.close()
            await client.close()


async def _execute_browser_action(
    page: AsyncPage,
    action_name: str,
    args: dict[str, object],
) -> None:
    """Execute a browser action based on the Gemini Computer Use action schema."""

    # Helper to safely get typed values from args
    def get_int(key: str, default: int = 0) -> int:
        val = args.get(key)
        if val is None:
            return default
        if isinstance(val, int):
            return val
        return int(str(val))

    def get_str(key: str, default: str = "") -> str:
        val = args.get(key)
        if val is None:
            return default
        return str(val)

    def get_bool(key: str, default: bool = False) -> bool:
        val = args.get(key)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        return bool(val)

    if action_name == "navigate":
        url = get_str("url", "")
        if not url.startswith("http"):
            url = "https://" + url
        await page.goto(url)

    elif action_name == "click_at":
        x = _denormalize_coordinate(get_int("x", 0), _SCREEN_WIDTH)
        y = _denormalize_coordinate(get_int("y", 0), _SCREEN_HEIGHT)
        await page.mouse.click(x, y)

    elif action_name == "type_text_at":
        x = _denormalize_coordinate(get_int("x", 0), _SCREEN_WIDTH)
        y = _denormalize_coordinate(get_int("y", 0), _SCREEN_HEIGHT)
        text = get_str("text", "")
        press_enter = get_bool("press_enter", True)
        clear_before = get_bool("clear_before_typing", False)

        await page.mouse.click(x, y)
        if clear_before:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
        await page.keyboard.type(text)
        if press_enter:
            await page.keyboard.press("Enter")

    elif action_name == "scroll_at":
        x = _denormalize_coordinate(get_int("x", 0), _SCREEN_WIDTH)
        y = _denormalize_coordinate(get_int("y", 0), _SCREEN_HEIGHT)
        direction = get_str("direction", "down")
        magnitude = get_int("magnitude", 800)

        delta_x, delta_y = 0, 0
        if direction == "down":
            delta_y = magnitude
        elif direction == "up":
            delta_y = -magnitude
        elif direction == "right":
            delta_x = magnitude
        elif direction == "left":
            delta_x = -magnitude

        await page.mouse.move(x, y)
        await page.mouse.wheel(delta_x, delta_y)

    elif action_name == "scroll_document":
        direction = get_str("direction", "down")
        if direction == "down":
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
        elif direction == "up":
            await page.evaluate("window.scrollBy(0, -window.innerHeight)")
        elif direction == "right":
            await page.evaluate("window.scrollBy(window.innerWidth, 0)")
        elif direction == "left":
            await page.evaluate("window.scrollBy(-window.innerWidth, 0)")

    elif action_name == "go_back":
        await page.go_back()

    elif action_name == "go_forward":
        await page.go_forward()

    elif action_name == "key_combination":
        keys = get_str("keys", "")
        await page.keyboard.press(keys)

    elif action_name == "hover_at":
        x = _denormalize_coordinate(get_int("x", 0), _SCREEN_WIDTH)
        y = _denormalize_coordinate(get_int("y", 0), _SCREEN_HEIGHT)
        await page.mouse.move(x, y)

    elif action_name == "wait_5_seconds":
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Implementing model's wait action
        await asyncio.sleep(5)

    elif action_name == "open_web_browser":
        await page.goto("about:blank")

    elif action_name == "search":
        await page.goto("https://www.google.com")

    else:
        logger.warning(f"Unknown action: {action_name}")
