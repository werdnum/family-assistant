"""Computer Use tools for browser automation using Playwright.

This module implements the tools required by the Gemini Computer Use model
to interact with a web browser using async Playwright.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from family_assistant.tools.types import ToolAttachment, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Default screen dimensions for Gemini Computer Use
_SCREEN_WIDTH = 1024
_SCREEN_HEIGHT = 768


@dataclass
class BrowserSession:
    """Manages the browser lifecycle for computer use tools.

    This class encapsulates browser state in a clean, non-global way.
    Each session can be started, used, and closed independently.
    """

    playwright: Playwright | None = field(default=None, repr=False)
    browser: Browser | None = field(default=None, repr=False)
    context: BrowserContext | None = field(default=None, repr=False)
    page: Page | None = field(default=None, repr=False)
    screen_width: int = _SCREEN_WIDTH
    screen_height: int = _SCREEN_HEIGHT

    async def ensure_page(self) -> Page:
        """Ensure a browser page is available, creating one if necessary."""
        if self.page is not None:
            return self.page

        if self.playwright is None:
            self.playwright = await async_playwright().start()

        if self.browser is None:
            self.browser = await self.playwright.chromium.launch(headless=True)

        if self.context is None:
            self.context = await self.browser.new_context(
                viewport={"width": self.screen_width, "height": self.screen_height}
            )

        if self.page is None:
            self.page = await self.context.new_page()

        return self.page

    async def close(self) -> None:
        """Close all browser resources."""
        if self.context is not None:
            await self.context.close()
            self.context = None
            self.page = None

        if self.browser is not None:
            await self.browser.close()
            self.browser = None

        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None


# Session storage keyed by conversation_id for multi-user support
_sessions: dict[str, BrowserSession] = {}


async def get_browser_session(exec_context: ToolExecutionContext) -> BrowserSession:
    """Get or create a browser session for the given execution context."""
    session_key = exec_context.conversation_id or "default"
    if session_key not in _sessions:
        _sessions[session_key] = BrowserSession()
    return _sessions[session_key]


async def close_browser_session(exec_context: ToolExecutionContext) -> None:
    """Close and remove the browser session for the given execution context."""
    session_key = exec_context.conversation_id or "default"
    if session_key in _sessions:
        await _sessions[session_key].close()
        del _sessions[session_key]


def _denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)


async def _take_screenshot_with_url(page: Page) -> ToolResult:
    """Take a screenshot and return it as a ToolResult with URL.

    The Gemini Computer Use model requires function responses to include
    the URL of the current web page along with the screenshot.
    """
    screenshot_bytes = await page.screenshot(type="png")
    attachment = ToolAttachment(
        content=screenshot_bytes,
        mime_type="image/png",
        description="Browser screenshot",
    )
    return ToolResult(
        data={"url": page.url},
        attachments=[attachment],
    )


# --- Tool Implementations ---


async def computer_use_click_at(
    exec_context: ToolExecutionContext, x: int, y: int
) -> ToolResult:
    """Click at a specific coordinate on the screen.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).

    Returns:
        A screenshot of the screen after the click.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    actual_x = _denormalize_coordinate(x, session.screen_width)
    actual_y = _denormalize_coordinate(y, session.screen_height)

    logger.info(f"Clicking at ({actual_x}, {actual_y})")
    await page.mouse.click(actual_x, actual_y)

    # Wait for potential navigations/renders
    with contextlib.suppress(Exception):
        await page.wait_for_load_state(timeout=2000)

    return await _take_screenshot_with_url(page)


async def computer_use_type_text_at(
    exec_context: ToolExecutionContext,
    x: int,
    y: int,
    text: str,
    press_enter: bool = True,
) -> ToolResult:
    """Type text at a specific coordinate.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).
        text: The text to type.
        press_enter: Whether to press Enter after typing.

    Returns:
        A screenshot of the screen after typing.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    actual_x = _denormalize_coordinate(x, session.screen_width)
    actual_y = _denormalize_coordinate(y, session.screen_height)

    logger.info(f"Typing '{text}' at ({actual_x}, {actual_y})")

    # Click to focus
    await page.mouse.click(actual_x, actual_y)

    # Clear existing text (Ctrl+A + Backspace)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")

    await page.keyboard.type(text)

    if press_enter:
        await page.keyboard.press("Enter")

    # Wait for potential navigations/renders
    with contextlib.suppress(Exception):
        await page.wait_for_load_state(timeout=2000)

    return await _take_screenshot_with_url(page)


async def computer_use_scroll_at(
    exec_context: ToolExecutionContext,
    x: int,
    y: int,
    direction: str,
    magnitude: int = 800,
) -> ToolResult:
    """Scroll the screen at a specific coordinate.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).
        direction: "up", "down", "left", "right".
        magnitude: The amount to scroll (default 800).

    Returns:
        A screenshot of the screen after scrolling.

    Raises:
        ValueError: If direction is not one of "up", "down", "left", "right".
    """
    valid_directions = ("up", "down", "left", "right")
    if direction not in valid_directions:
        raise ValueError(
            f"Invalid scroll direction '{direction}'. Must be one of: {valid_directions}"
        )

    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    actual_x = _denormalize_coordinate(x, session.screen_width)
    actual_y = _denormalize_coordinate(y, session.screen_height)

    logger.info(f"Scrolling {direction} at ({actual_x}, {actual_y}) by {magnitude}")

    # Calculate delta based on direction
    delta_x = 0
    delta_y = 0

    if direction == "down":
        delta_y = magnitude
    elif direction == "up":
        delta_y = -magnitude
    elif direction == "right":
        delta_x = magnitude
    elif direction == "left":
        delta_x = -magnitude

    await page.mouse.move(actual_x, actual_y)
    await page.mouse.wheel(delta_x, delta_y)

    await asyncio.sleep(0.5)  # Wait for scroll animation

    return await _take_screenshot_with_url(page)


async def computer_use_open_web_browser(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Open the web browser with a default search page.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the browser showing Google.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    # Navigate to Google as the default starting page
    # Computer Use API requires a valid HTTP/HTTPS URL
    logger.info("Opening web browser (navigating to Google)")
    await page.goto("https://www.google.com")
    return await _take_screenshot_with_url(page)


async def computer_use_navigate(
    exec_context: ToolExecutionContext, url: str
) -> ToolResult:
    """Navigate to a URL.

    Args:
        exec_context: The tool execution context.
        url: The URL to navigate to.

    Returns:
        A screenshot of the page after navigation.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info(f"Navigating to {url}")

    # Ensure URL has protocol
    if not url.startswith("http"):
        url = "https://" + url

    await page.goto(url)
    return await _take_screenshot_with_url(page)


async def computer_use_search(exec_context: ToolExecutionContext) -> ToolResult:
    """Navigate to the default search engine.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the search engine homepage.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info("Navigating to search engine")
    await page.goto("https://www.google.com")
    return await _take_screenshot_with_url(page)


async def computer_use_go_back(exec_context: ToolExecutionContext) -> ToolResult:
    """Navigate back in history.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the page after navigation.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info("Going back")
    await page.go_back()
    return await _take_screenshot_with_url(page)


async def computer_use_go_forward(exec_context: ToolExecutionContext) -> ToolResult:
    """Navigate forward in history.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the page after navigation.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info("Going forward")
    await page.go_forward()
    return await _take_screenshot_with_url(page)


async def computer_use_key_combination(
    exec_context: ToolExecutionContext, keys: str
) -> ToolResult:
    """Press a key combination.

    Args:
        exec_context: The tool execution context.
        keys: The key combination (e.g. 'Control+C', 'Enter').

    Returns:
        A screenshot of the screen after the key press.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info(f"Pressing keys: {keys}")
    await page.keyboard.press(keys)
    return await _take_screenshot_with_url(page)


async def computer_use_wait_5_seconds(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Wait for 5 seconds.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the screen after waiting.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info("Waiting 5 seconds")
    await asyncio.sleep(5)
    return await _take_screenshot_with_url(page)


async def computer_use_hover_at(
    exec_context: ToolExecutionContext, x: int, y: int
) -> ToolResult:
    """Hover the mouse at a specific coordinate.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).

    Returns:
        A screenshot of the screen after hovering.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    actual_x = _denormalize_coordinate(x, session.screen_width)
    actual_y = _denormalize_coordinate(y, session.screen_height)

    logger.info(f"Hovering at ({actual_x}, {actual_y})")
    await page.mouse.move(actual_x, actual_y)

    return await _take_screenshot_with_url(page)


async def computer_use_drag_and_drop(
    exec_context: ToolExecutionContext,
    x: int,
    y: int,
    destination_x: int,
    destination_y: int,
) -> ToolResult:
    """Drag an element from one coordinate to another.

    Args:
        exec_context: The tool execution context.
        x: Start x coordinate (0-1000).
        y: Start y coordinate (0-1000).
        destination_x: End x coordinate (0-1000).
        destination_y: End y coordinate (0-1000).

    Returns:
        A screenshot of the screen after the drag and drop.
    """
    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    start_x = _denormalize_coordinate(x, session.screen_width)
    start_y = _denormalize_coordinate(y, session.screen_height)
    end_x = _denormalize_coordinate(destination_x, session.screen_width)
    end_y = _denormalize_coordinate(destination_y, session.screen_height)

    logger.info(f"Dragging from ({start_x}, {start_y}) to ({end_x}, {end_y})")

    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(
        end_x, end_y, steps=10
    )  # Steps make it more realistic/reliable
    await page.mouse.up()

    return await _take_screenshot_with_url(page)


async def computer_use_scroll_document(
    exec_context: ToolExecutionContext, direction: str
) -> ToolResult:
    """Scroll the entire document.

    Args:
        exec_context: The tool execution context.
        direction: "up", "down", "left", "right".

    Returns:
        A screenshot of the screen after scrolling.

    Raises:
        ValueError: If direction is not one of "up", "down", "left", "right".
    """
    valid_directions = ("up", "down", "left", "right")
    if direction not in valid_directions:
        raise ValueError(
            f"Invalid scroll direction '{direction}'. Must be one of: {valid_directions}"
        )

    session = await get_browser_session(exec_context)
    page = await session.ensure_page()

    logger.info(f"Scrolling document {direction}")

    if direction == "down":
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
    elif direction == "up":
        await page.evaluate("window.scrollBy(0, -window.innerHeight)")
    elif direction == "right":
        await page.evaluate("window.scrollBy(window.innerWidth, 0)")
    elif direction == "left":
        await page.evaluate("window.scrollBy(-window.innerWidth, 0)")

    await asyncio.sleep(0.5)
    return await _take_screenshot_with_url(page)


# Tools Definition
COMPUTER_USE_TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": "Clicks at a specific coordinate on the webpage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-1000)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-1000)"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text_at",
            "description": "Types text at a specific coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-1000)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-1000)"},
                    "text": {"type": "string", "description": "Text to type"},
                    "press_enter": {
                        "type": "boolean",
                        "description": "Press Enter after typing",
                        "default": True,
                    },
                },
                "required": ["x", "y", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_at",
            "description": "Scrolls a specific element or area.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-1000)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-1000)"},
                    "direction": {
                        "type": "string",
                        "description": "Direction (up, down, left, right)",
                    },
                    "magnitude": {
                        "type": "integer",
                        "description": "Scroll amount",
                        "default": 800,
                    },
                },
                "required": ["x", "y", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_web_browser",
            "description": "Opens the web browser.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigates to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Navigates to the default search engine.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Navigates back in history.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_forward",
            "description": "Navigates forward in history.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_combination",
            "description": "Presses a key combination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "Key combination (e.g. 'Control+C')",
                    },
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_5_seconds",
            "description": "Waits for 5 seconds.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover_at",
            "description": "Hovers the mouse at a coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-1000)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-1000)"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag_and_drop",
            "description": "Drags an element to a new location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Start X (0-1000)"},
                    "y": {"type": "integer", "description": "Start Y (0-1000)"},
                    "destination_x": {
                        "type": "integer",
                        "description": "End X (0-1000)",
                    },
                    "destination_y": {
                        "type": "integer",
                        "description": "End Y (0-1000)",
                    },
                },
                "required": ["x", "y", "destination_x", "destination_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_document",
            "description": "Scrolls the entire document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "description": "Direction (up, down, left, right)",
                    },
                },
                "required": ["direction"],
            },
        },
    },
]
