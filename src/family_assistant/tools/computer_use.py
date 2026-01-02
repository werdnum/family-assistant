"""Computer Use tools for browser automation using Playwright.

This module implements the tools required by the Gemini Computer Use model
to interact with a web browser.
"""

import contextlib
import logging
import time

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from family_assistant.tools.types import ToolAttachment

logger = logging.getLogger(__name__)

# Global Playwright state
_PLAYWRIGHT = None
_BROWSER: Browser | None = None
_CONTEXT: BrowserContext | None = None
_PAGE: Page | None = None
_SCREEN_WIDTH = 1024
_SCREEN_HEIGHT = 768


def _get_page() -> Page:
    """Get or create the global Playwright page."""
    global _PLAYWRIGHT, _BROWSER, _CONTEXT, _PAGE

    if _PAGE:
        return _PAGE

    if not _PLAYWRIGHT:
        _PLAYWRIGHT = sync_playwright().start()

    if not _BROWSER:
        _BROWSER = _PLAYWRIGHT.chromium.launch(headless=True)

    if not _CONTEXT:
        # Use the recommended screen size for Gemini Computer Use
        _CONTEXT = _BROWSER.new_context(
            viewport={"width": _SCREEN_WIDTH, "height": _SCREEN_HEIGHT}
        )

    if not _PAGE:
        _PAGE = _CONTEXT.new_page()

    return _PAGE


def _take_screenshot(page: Page) -> ToolAttachment:
    """Take a screenshot and return it as a ToolAttachment."""
    screenshot_bytes = page.screenshot(type="png")
    return ToolAttachment(
        content=screenshot_bytes,
        mime_type="image/png",
        description="Browser screenshot",
    )


def _denormalize_coordinate(value: int, max_value: int) -> int:
    """Convert normalized coordinate (0-1000) to pixel value."""
    return int(value / 1000 * max_value)


# --- Tool Implementations ---


def computer_use_click_at(x: int, y: int) -> ToolAttachment:
    """Click at a specific coordinate on the screen.

    Args:
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).

    Returns:
        A screenshot of the screen after the click.
    """
    page = _get_page()
    actual_x = _denormalize_coordinate(x, _SCREEN_WIDTH)
    actual_y = _denormalize_coordinate(y, _SCREEN_HEIGHT)

    logger.info(f"Clicking at ({actual_x}, {actual_y})")
    page.mouse.click(actual_x, actual_y)

    # Wait for potential navigations/renders
    with contextlib.suppress(Exception):
        page.wait_for_load_state(timeout=2000)

    return _take_screenshot(page)


def computer_use_type_text_at(
    x: int, y: int, text: str, press_enter: bool = True
) -> ToolAttachment:
    """Type text at a specific coordinate.

    Args:
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).
        text: The text to type.
        press_enter: Whether to press Enter after typing.

    Returns:
        A screenshot of the screen after typing.
    """
    page = _get_page()
    actual_x = _denormalize_coordinate(x, _SCREEN_WIDTH)
    actual_y = _denormalize_coordinate(y, _SCREEN_HEIGHT)

    logger.info(f"Typing '{text}' at ({actual_x}, {actual_y})")

    # Click to focus
    page.mouse.click(actual_x, actual_y)

    # Clear existing text (Ctrl+A / Cmd+A + Backspace)
    # Using 'Control' for Linux/Windows, might need 'Meta' for Mac if running locally there
    # But inside container it's likely Linux.
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")

    page.keyboard.type(text)

    if press_enter:
        page.keyboard.press("Enter")

    # Wait for potential navigations/renders
    with contextlib.suppress(Exception):
        page.wait_for_load_state(timeout=2000)

    return _take_screenshot(page)


def computer_use_scroll_at(
    x: int, y: int, direction: str, magnitude: int = 800
) -> ToolAttachment:
    """Scroll the screen at a specific coordinate.

    Args:
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).
        direction: "up", "down", "left", "right".
        magnitude: The amount to scroll (default 800).

    Returns:
        A screenshot of the screen after scrolling.
    """
    page = _get_page()
    actual_x = _denormalize_coordinate(x, _SCREEN_WIDTH)
    actual_y = _denormalize_coordinate(y, _SCREEN_HEIGHT)

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

    page.mouse.move(actual_x, actual_y)
    page.mouse.wheel(delta_x, delta_y)

    time.sleep(0.5)  # Wait for scroll animation

    return _take_screenshot(page)


def computer_use_open_web_browser() -> ToolAttachment:
    """Open the web browser (or reset to a blank page).

    Returns:
        A screenshot of the blank browser.
    """
    page = _get_page()
    logger.info("Opening web browser (resetting to about:blank)")
    page.goto("about:blank")
    return _take_screenshot(page)


def computer_use_navigate(url: str) -> ToolAttachment:
    """Navigate to a URL.

    Args:
        url: The URL to navigate to.

    Returns:
        A screenshot of the page after navigation.
    """
    page = _get_page()
    logger.info(f"Navigating to {url}")

    # Ensure URL has protocol
    if not url.startswith("http"):
        url = "https://" + url

    page.goto(url)
    return _take_screenshot(page)


def computer_use_search() -> ToolAttachment:
    """Navigate to the default search engine.

    Returns:
        A screenshot of the search engine homepage.
    """
    page = _get_page()
    logger.info("Navigating to search engine")
    page.goto("https://www.google.com")
    return _take_screenshot(page)


def computer_use_go_back() -> ToolAttachment:
    """Navigate back in history.

    Returns:
        A screenshot of the page after navigation.
    """
    page = _get_page()
    logger.info("Going back")
    page.go_back()
    return _take_screenshot(page)


def computer_use_go_forward() -> ToolAttachment:
    """Navigate forward in history.

    Returns:
        A screenshot of the page after navigation.
    """
    page = _get_page()
    logger.info("Going forward")
    page.go_forward()
    return _take_screenshot(page)


def computer_use_key_combination(keys: str) -> ToolAttachment:
    """Press a key combination.

    Args:
        keys: The key combination (e.g. 'Control+C', 'Enter').

    Returns:
        A screenshot of the screen after the key press.
    """
    page = _get_page()
    logger.info(f"Pressing keys: {keys}")
    page.keyboard.press(keys)
    return _take_screenshot(page)


def computer_use_wait_5_seconds() -> ToolAttachment:
    """Wait for 5 seconds.

    Returns:
        A screenshot of the screen after waiting.
    """
    page = _get_page()
    logger.info("Waiting 5 seconds")
    time.sleep(5)
    return _take_screenshot(page)


def computer_use_hover_at(x: int, y: int) -> ToolAttachment:
    """Hover the mouse at a specific coordinate.

    Args:
        x: The x coordinate (0-1000).
        y: The y coordinate (0-1000).

    Returns:
        A screenshot of the screen after hovering.
    """
    page = _get_page()
    actual_x = _denormalize_coordinate(x, _SCREEN_WIDTH)
    actual_y = _denormalize_coordinate(y, _SCREEN_HEIGHT)

    logger.info(f"Hovering at ({actual_x}, {actual_y})")
    page.mouse.move(actual_x, actual_y)

    return _take_screenshot(page)


def computer_use_drag_and_drop(
    x: int, y: int, destination_x: int, destination_y: int
) -> ToolAttachment:
    """Drag an element from one coordinate to another.

    Args:
        x: Start x coordinate (0-1000).
        y: Start y coordinate (0-1000).
        destination_x: End x coordinate (0-1000).
        destination_y: End y coordinate (0-1000).

    Returns:
        A screenshot of the screen after the drag and drop.
    """
    page = _get_page()
    start_x = _denormalize_coordinate(x, _SCREEN_WIDTH)
    start_y = _denormalize_coordinate(y, _SCREEN_HEIGHT)
    end_x = _denormalize_coordinate(destination_x, _SCREEN_WIDTH)
    end_y = _denormalize_coordinate(destination_y, _SCREEN_HEIGHT)

    logger.info(f"Dragging from ({start_x}, {start_y}) to ({end_x}, {end_y})")

    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(end_x, end_y, steps=10)  # Steps make it more realistic/reliable
    page.mouse.up()

    return _take_screenshot(page)


def computer_use_scroll_document(direction: str) -> ToolAttachment:
    """Scroll the entire document.

    Args:
        direction: "up", "down", "left", "right".

    Returns:
        A screenshot of the screen after scrolling.
    """
    # This might need JS execution to scroll document reliably
    page = _get_page()
    logger.info(f"Scrolling document {direction}")

    if direction == "down":
        page.evaluate("window.scrollBy(0, window.innerHeight)")
    elif direction == "up":
        page.evaluate("window.scrollBy(0, -window.innerHeight)")
    elif direction == "right":
        page.evaluate("window.scrollBy(window.innerWidth, 0)")
    elif direction == "left":
        page.evaluate("window.scrollBy(-window.innerWidth, 0)")

    time.sleep(0.5)
    return _take_screenshot(page)


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
