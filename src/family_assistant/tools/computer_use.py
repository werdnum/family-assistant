"""Computer Use tools for browser automation (Gemini 3.5 native action space).

This module implements the tools required by the Gemini 3.5 Computer Use native model
to interact with a web browser. All operations go through
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
    "computer_use_click",
    "computer_use_double_click",
    "computer_use_drag_and_drop",
    "computer_use_go_back",
    "computer_use_go_forward",
    "computer_use_hotkey",
    "computer_use_key_down",
    "computer_use_key_up",
    "computer_use_middle_click",
    "computer_use_mouse_down",
    "computer_use_mouse_up",
    "computer_use_move",
    "computer_use_navigate",
    "computer_use_press_key",
    "computer_use_right_click",
    "computer_use_scroll",
    "computer_use_take_screenshot",
    "computer_use_triple_click",
    "computer_use_type",
    "computer_use_wait",
    "get_browser_session",
]


def _map_key_name(key: str) -> str:
    """Map model-emitted key names to Playwright key names.

    Gemini emits lowercase names like "ctrl", "enter", "esc", "page_down";
    Playwright wants "Control", "Enter", "Escape", "PageDown", etc.

    Args:
        key: Key name emitted by the model (case-insensitive).

    Returns:
        Mapped key name suitable for Playwright.
    """
    key_lower = key.lower()

    # Direct single-character mappings pass through
    if len(key_lower) == 1:
        return key

    # Multi-character key mappings
    mapping = {
        "ctrl": "Control",
        "control": "Control",
        "alt": "Alt",
        "option": "Alt",
        "shift": "Shift",
        "meta": "Meta",
        "cmd": "Meta",
        "command": "Meta",
        "win": "Meta",
        "super": "Meta",
        "enter": "Enter",
        "return": "Enter",
        "esc": "Escape",
        "escape": "Escape",
        "tab": "Tab",
        "space": "Space",
        "backspace": "Backspace",
        "delete": "Delete",
        "del": "Delete",
        "insert": "Insert",
        "home": "Home",
        "end": "End",
        "pageup": "PageUp",
        "page_up": "PageUp",
        "pagedown": "PageDown",
        "page_down": "PageDown",
        "up": "ArrowUp",
        "arrowup": "ArrowUp",
        "arrow_up": "ArrowUp",
        "down": "ArrowDown",
        "arrowdown": "ArrowDown",
        "arrow_down": "ArrowDown",
        "left": "ArrowLeft",
        "arrowleft": "ArrowLeft",
        "arrow_left": "ArrowLeft",
        "right": "ArrowRight",
        "arrowright": "ArrowRight",
        "arrow_right": "ArrowRight",
    }

    # Function keys f1..f12
    if key_lower.startswith("f") and len(key_lower) <= 3:
        try:
            num = int(key_lower[1:])
            if 1 <= num <= 12:
                return f"F{num}"
        except ValueError:
            pass

    # Return mapped value or capitalize first letter as fallback
    return mapping.get(key_lower, key[0].upper() + key[1:] if key else key)


async def _take_screenshot_with_url(backend: BrowserBackend) -> ToolResult:
    """Take a screenshot and return it as a ToolResult with URL.

    The Gemini Computer Use model requires function responses to include
    the URL of the current web page along with the screenshot.

    Every Computer Use action is assumed to have potentially mutated the
    page (click, type, scroll, navigate, …), so any DOM refs captured by
    ``browser_dom`` snapshots on the shared session are now stale. We
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


# --- Tool Implementations (Gemini 3.5 action space) ---


async def computer_use_click(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Click at a specific coordinate on the screen.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the click.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Clicking at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y)
    return await _take_screenshot_with_url(backend)


async def computer_use_double_click(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Double-click at a specific coordinate on the screen.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the double-click.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Double-clicking at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y, click_count=2)
    return await _take_screenshot_with_url(backend)


async def computer_use_triple_click(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Triple-click at a specific coordinate on the screen.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the triple-click.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Triple-clicking at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y, click_count=3)
    return await _take_screenshot_with_url(backend)


async def computer_use_middle_click(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Middle-click at a specific coordinate on the screen.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the middle-click.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Middle-clicking at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y, button="middle")
    return await _take_screenshot_with_url(backend)


async def computer_use_right_click(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Right-click at a specific coordinate on the screen.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the right-click.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Right-clicking at ({actual_x}, {actual_y})")
    await backend.mouse_click(actual_x, actual_y, button="right")
    return await _take_screenshot_with_url(backend)


async def computer_use_mouse_down(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Press mouse button down at a specific coordinate.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after pressing down.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Pressing mouse button down at ({actual_x}, {actual_y})")
    await backend.mouse_move(actual_x, actual_y)
    await backend.mouse_down()
    return await _take_screenshot_with_url(backend)


async def computer_use_mouse_up(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Release mouse button at a specific coordinate.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after releasing.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Releasing mouse button at ({actual_x}, {actual_y})")
    await backend.mouse_move(actual_x, actual_y)
    await backend.mouse_up()
    return await _take_screenshot_with_url(backend)


async def computer_use_move(
    exec_context: ToolExecutionContext, x: int, y: int, intent: str = ""
) -> ToolResult:
    """Move mouse to a specific coordinate without clicking.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after moving.
    """
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(f"Moving mouse to ({actual_x}, {actual_y})")
    await backend.mouse_move(actual_x, actual_y)
    return await _take_screenshot_with_url(backend)


async def computer_use_type(
    exec_context: ToolExecutionContext,
    text: str,
    press_enter: bool = False,
    intent: str = "",
) -> ToolResult:
    """Type text at the current focus without clicking.

    Args:
        exec_context: The tool execution context.
        text: The text to type.
        press_enter: Whether to press Enter after typing.
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after typing.
    """
    backend = await get_browser_backend(exec_context)
    logger.info(f"Typing '{text}'")
    await backend.keyboard_type(text)
    if press_enter:
        await backend.keyboard_press("Enter")
    return await _take_screenshot_with_url(backend)


async def computer_use_press_key(
    exec_context: ToolExecutionContext, key: str, intent: str = ""
) -> ToolResult:
    """Press a single key.

    Args:
        exec_context: The tool execution context.
        key: The key to press (e.g., 'Enter', 'Escape').
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the key press.
    """
    backend = await get_browser_backend(exec_context)
    mapped_key = _map_key_name(key)
    logger.info(f"Pressing key: {mapped_key}")
    await backend.keyboard_press(mapped_key)
    return await _take_screenshot_with_url(backend)


async def computer_use_key_down(
    exec_context: ToolExecutionContext, key: str, intent: str = ""
) -> ToolResult:
    """Press and hold a key down.

    Args:
        exec_context: The tool execution context.
        key: The key to press down (e.g., 'Control', 'Shift').
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen.

    Raises:
        BrowserBackendError: If the backend doesn't support key-down.
    """
    backend = await get_browser_backend(exec_context)
    mapped_key = _map_key_name(key)
    logger.info(f"Pressing key down: {mapped_key}")
    await backend.keyboard_down(mapped_key)
    return await _take_screenshot_with_url(backend)


async def computer_use_key_up(
    exec_context: ToolExecutionContext, key: str, intent: str = ""
) -> ToolResult:
    """Release a key that was pressed down.

    Args:
        exec_context: The tool execution context.
        key: The key to release (e.g., 'Control', 'Shift').
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen.

    Raises:
        BrowserBackendError: If the backend doesn't support key-up.
    """
    backend = await get_browser_backend(exec_context)
    mapped_key = _map_key_name(key)
    logger.info(f"Releasing key: {mapped_key}")
    await backend.keyboard_up(mapped_key)
    return await _take_screenshot_with_url(backend)


async def computer_use_hotkey(
    exec_context: ToolExecutionContext, keys: list[str], intent: str = ""
) -> ToolResult:
    """Press multiple keys simultaneously (hotkey).

    Args:
        exec_context: The tool execution context.
        keys: List of key names to press together.
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the hotkey.
    """
    backend = await get_browser_backend(exec_context)
    mapped_keys = [_map_key_name(k) for k in keys]
    combined = "+".join(mapped_keys)
    logger.info(f"Pressing hotkey: {combined}")
    await backend.keyboard_press(combined)
    return await _take_screenshot_with_url(backend)


async def computer_use_scroll(
    exec_context: ToolExecutionContext,
    x: int,
    y: int,
    direction: str,
    magnitude_in_pixels: int = 300,
    intent: str = "",
) -> ToolResult:
    """Scroll in a specific direction at a coordinate.

    Args:
        exec_context: The tool execution context.
        x: The x coordinate (0-999).
        y: The y coordinate (0-999).
        direction: Direction to scroll ("up", "down", "left", "right").
        magnitude_in_pixels: Amount to scroll (default 300).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after scrolling.

    Raises:
        ValueError: If direction is not one of "up", "down", "left", "right".
    """
    valid_directions = {"up", "down", "left", "right"}
    if direction not in valid_directions:
        raise ValueError(
            f"Invalid scroll direction '{direction}'. Must be one of: {valid_directions}"
        )
    backend = await get_browser_backend(exec_context)
    actual_x = denormalize_coordinate(x, backend.screen_width)
    actual_y = denormalize_coordinate(y, backend.screen_height)
    logger.info(
        f"Scrolling {direction} at ({actual_x}, {actual_y}) by {magnitude_in_pixels}"
    )
    delta_x = 0.0
    delta_y = 0.0
    if direction == "down":
        delta_y = float(magnitude_in_pixels)
    elif direction == "up":
        delta_y = -float(magnitude_in_pixels)
    elif direction == "right":
        delta_x = float(magnitude_in_pixels)
    elif direction == "left":
        delta_x = -float(magnitude_in_pixels)
    await backend.mouse_move(actual_x, actual_y)
    await backend.mouse_wheel(delta_x, delta_y)
    await asyncio.sleep(0.5)
    return await _take_screenshot_with_url(backend)


async def computer_use_drag_and_drop(
    exec_context: ToolExecutionContext,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    intent: str = "",
) -> ToolResult:
    """Drag an element from one coordinate to another.

    Args:
        exec_context: The tool execution context.
        start_x: Start x coordinate (0-999).
        start_y: Start y coordinate (0-999).
        end_x: End x coordinate (0-999).
        end_y: End y coordinate (0-999).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after the drag and drop.
    """
    backend = await get_browser_backend(exec_context)
    actual_start_x = denormalize_coordinate(start_x, backend.screen_width)
    actual_start_y = denormalize_coordinate(start_y, backend.screen_height)
    actual_end_x = denormalize_coordinate(end_x, backend.screen_width)
    actual_end_y = denormalize_coordinate(end_y, backend.screen_height)
    logger.info(
        f"Dragging from ({actual_start_x}, {actual_start_y}) to ({actual_end_x}, {actual_end_y})"
    )
    await backend.mouse_move(actual_start_x, actual_start_y)
    await backend.mouse_down()
    # Move in steps for realism/reliability
    step_count = 10
    for i in range(1, step_count + 1):
        ix = actual_start_x + (actual_end_x - actual_start_x) * i / step_count
        iy = actual_start_y + (actual_end_y - actual_start_y) * i / step_count
        await backend.mouse_move(ix, iy)
    await backend.mouse_up()
    return await _take_screenshot_with_url(backend)


async def computer_use_navigate(
    exec_context: ToolExecutionContext, url: str, intent: str = ""
) -> ToolResult:
    """Navigate to a URL.

    Args:
        exec_context: The tool execution context.
        url: The URL to navigate to.
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the page after navigation.
    """
    backend = await get_browser_backend(exec_context)
    logger.info(f"Navigating to {url}")
    if not url.startswith("http"):
        url = "https://" + url
    await backend.goto(url)
    return await _take_screenshot_with_url(backend)


async def computer_use_go_back(
    exec_context: ToolExecutionContext, intent: str = ""
) -> ToolResult:
    """Navigate back in history.

    Args:
        exec_context: The tool execution context.
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the page after navigation.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Going back")
    await backend.go_back()
    return await _take_screenshot_with_url(backend)


async def computer_use_go_forward(
    exec_context: ToolExecutionContext, intent: str = ""
) -> ToolResult:
    """Navigate forward in history.

    Args:
        exec_context: The tool execution context.
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the page after navigation.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Going forward")
    await backend.go_forward()
    return await _take_screenshot_with_url(backend)


async def computer_use_take_screenshot(
    exec_context: ToolExecutionContext, intent: str = ""
) -> ToolResult:
    """Take a screenshot of the current screen.

    Args:
        exec_context: The tool execution context.
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen.
    """
    backend = await get_browser_backend(exec_context)
    logger.info("Taking screenshot")
    return await _take_screenshot_with_url(backend)


async def computer_use_wait(
    exec_context: ToolExecutionContext, seconds: int = 1, intent: str = ""
) -> ToolResult:
    """Wait for a specified number of seconds.

    Args:
        exec_context: The tool execution context.
        seconds: Number of seconds to wait (clamped to 0-30 to guard against runaway sleeps).
        intent: Model-stated intent for this action (unused).

    Returns:
        A screenshot of the screen after waiting.
    """
    backend = await get_browser_backend(exec_context)
    # Clamp to reasonable range to guard against runaway sleeps
    clamped_seconds = max(0, min(30, seconds))
    logger.info(f"Waiting {clamped_seconds} seconds")
    await asyncio.sleep(clamped_seconds)
    return await _take_screenshot_with_url(backend)


# Tools Definition
COMPUTER_USE_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click at a specific coordinate on the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Double-click at a specific coordinate on the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "triple_click",
            "description": "Triple-click at a specific coordinate on the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "middle_click",
            "description": "Middle-click at a specific coordinate on the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click",
            "description": "Right-click at a specific coordinate on the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_down",
            "description": "Press mouse button down at a specific coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_up",
            "description": "Release mouse button at a specific coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "Move mouse to a specific coordinate without clicking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Type text at the current focus without clicking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                    "press_enter": {
                        "type": "boolean",
                        "description": "Press Enter after typing",
                        "default": False,
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a single key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key to press (e.g., 'Enter', 'Escape')",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_down",
            "description": "Press and hold a key down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key to press down (e.g., 'Control', 'Shift')",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_up",
            "description": "Release a key that was pressed down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key to release (e.g., 'Control', 'Shift')",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Press multiple keys simultaneously (hotkey).",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of key names to press together",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll in a specific direction at a coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"},
                    "direction": {
                        "type": "string",
                        "description": "Direction (up, down, left, right)",
                    },
                    "magnitude_in_pixels": {
                        "type": "integer",
                        "description": "Amount to scroll",
                        "default": 300,
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["x", "y", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag_and_drop",
            "description": "Drag an element from one coordinate to another.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "description": "Start X (0-999)"},
                    "start_y": {"type": "integer", "description": "Start Y (0-999)"},
                    "end_x": {"type": "integer", "description": "End X (0-999)"},
                    "end_y": {"type": "integer", "description": "End Y (0-999)"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate to a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Navigate back in history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_forward",
            "description": "Navigate forward in history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Take a screenshot of the current screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait for a specified number of seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Number of seconds to wait (0-30)",
                        "default": 1,
                    },
                    "intent": {
                        "type": "string",
                        "description": "Model-stated intent for this action",
                    },
                },
                "required": [],
            },
        },
    },
]
