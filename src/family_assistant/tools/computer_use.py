"""Computer Use tools for browser automation.

This module implements the tools required by the Gemini Computer Use model
to interact with a web browser.  All operations go through
:class:`~family_assistant.tools.browser_backend.BrowserBackend` so that when a
remote ``browser-server`` session is active the visual profile shares the
same live browser tab as the semantic DOM profile.

When no remote backend is configured the local Playwright session is used,
preserving the existing behaviour.

``BrowserSession``, ``get_browser_session``, and ``close_browser_session``
are re-exported for callers that still reference them directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from family_assistant.tools.browser_backend import BrowserBackend, get_browser_backend
from family_assistant.tools.browser_session import (
    BrowserSession,
    close_browser_session,
    denormalize_coordinate,
    get_browser_session,
)
from family_assistant.tools.types import ToolAttachment, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

__all__ = [
    "COMPUTER_USE_TOOLS_DEFINITION",
    "BrowserSession",
    "close_browser_session",
    "computer_use_click_at",
    "computer_use_drag_and_drop",
    "computer_use_go_back",
    "computer_use_go_forward",
    "computer_use_hover_at",
    "computer_use_key_combination",
    "computer_use_navigate",
    "computer_use_open_web_browser",
    "computer_use_scroll_at",
    "computer_use_scroll_document",
    "computer_use_search",
    "computer_use_type_text_at",
    "computer_use_wait_5_seconds",
    "get_browser_session",
]


async def _take_screenshot_with_url(backend: BrowserBackend) -> ToolResult:
    """Take a screenshot and return it as a ToolResult with URL.

    The Gemini Computer Use model requires function responses to include
    the URL of the current web page along with the screenshot.

    Every Computer Use action is assumed to have potentially mutated the
    page (click, type, scroll, navigate, …), so any DOM refs captured by
    ``browser_dom`` snapshots on the shared session are now stale.  We
    invalidate them here so that a subsequent ``browser_click`` can't
    target a ref that no longer points at the intended element.
    """
    backend.clear_refs()
    screenshot_bytes = await backend.screenshot_png()
    attachment = ToolAttachment(
        content=screenshot_bytes,
        mime_type="image/png",
        description="Browser screenshot",
    )
    return ToolResult(
        data={"url": backend.current_url},
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
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Clicking at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y)
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Typing '{text}' at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y)
    await backend.keyboard_press("Control+A")
    await backend.keyboard_press("Backspace")
    await backend.keyboard_type(text)
    if press_enter:
        await backend.keyboard_press("Enter")
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Scrolling {direction} at ({actual_x}, {actual_y}) by {magnitude}")
    delta_x = 0.0
    delta_y = 0.0
    if direction == "down":
        delta_y = float(magnitude)
    elif direction == "up":
        delta_y = -float(magnitude)
    elif direction == "right":
        delta_x = float(magnitude)
    elif direction == "left":
        delta_x = -float(magnitude)
    await backend.mouse_move(actual_x, actual_y)
    await backend.mouse_wheel(delta_x, delta_y)
    await asyncio.sleep(0.5)
    return await _take_screenshot_with_url(backend)


async def computer_use_open_web_browser(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Open the web browser with a default search page.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the browser showing Google.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Opening web browser (navigating to Google)")
    await backend.goto("https://www.google.com")
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    logger.info(f"Navigating to {url}")
    if not url.startswith("http"):
        url = "https://" + url
    await backend.goto(url)
    return await _take_screenshot_with_url(backend)


async def computer_use_search(exec_context: ToolExecutionContext) -> ToolResult:
    """Navigate to the default search engine.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the search engine homepage.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Navigating to search engine")
    await backend.goto("https://www.google.com")
    return await _take_screenshot_with_url(backend)


async def computer_use_go_back(exec_context: ToolExecutionContext) -> ToolResult:
    """Navigate back in history.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the page after navigation.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Going back")
    await backend.go_back()
    return await _take_screenshot_with_url(backend)


async def computer_use_go_forward(exec_context: ToolExecutionContext) -> ToolResult:
    """Navigate forward in history.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the page after navigation.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Going forward")
    await backend.go_forward()
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    logger.info(f"Pressing keys: {keys}")
    await backend.keyboard_press(keys)
    return await _take_screenshot_with_url(backend)


async def computer_use_wait_5_seconds(
    exec_context: ToolExecutionContext,
) -> ToolResult:
    """Wait for 5 seconds.

    Args:
        exec_context: The tool execution context.

    Returns:
        A screenshot of the screen after waiting.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Waiting 5 seconds")
    await asyncio.sleep(5)
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Hovering at ({actual_x}, {actual_y})")
    await backend.mouse_move(actual_x, actual_y)
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    start_x = denormalize_coordinate(x, backend.screen_width)
    start_y = denormalize_coordinate(y, backend.screen_height)
    end_x = denormalize_coordinate(destination_x, backend.screen_width)
    end_y = denormalize_coordinate(destination_y, backend.screen_height)
    logger.info(f"Dragging from ({start_x}, {start_y}) to ({end_x}, {end_y})")
    await backend.mouse_move(start_x, start_y)
    await backend.mouse_down()
    # Move in steps for realism/reliability
    step_count = 10
    for i in range(1, step_count + 1):
        ix = start_x + (end_x - start_x) * i / step_count
        iy = start_y + (end_y - start_y) * i / step_count
        await backend.mouse_move(ix, iy)
    await backend.mouse_up()
    return await _take_screenshot_with_url(backend)


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
    backend = await get_browser_backend(exec_context)
    logger.info(f"Scrolling document {direction}")
    scroll_js = {
        "down": "window.scrollBy(0, window.innerHeight)",
        "up": "window.scrollBy(0, -window.innerHeight)",
        "right": "window.scrollBy(window.innerWidth, 0)",
        "left": "window.scrollBy(-window.innerWidth, 0)",
    }
    await backend.evaluate(scroll_js[direction])
    await asyncio.sleep(0.5)
    return await _take_screenshot_with_url(backend)


# Tools Definition
COMPUTER_USE_TOOLS_DEFINITION: list[ToolDefinition] = [
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
