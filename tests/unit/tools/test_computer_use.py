"""Unit tests for Gemini 3.5 computer use tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import family_assistant.tools.computer_use as cu_module
from family_assistant.tools.browser_session import denormalize_coordinate
from family_assistant.tools.computer_use import (
    computer_use_click,
    computer_use_double_click,
    computer_use_drag_and_drop,
    computer_use_go_back,
    computer_use_go_forward,
    computer_use_hotkey,
    computer_use_key_down,
    computer_use_key_up,
    computer_use_middle_click,
    computer_use_mouse_down,
    computer_use_mouse_up,
    computer_use_move,
    computer_use_navigate,
    computer_use_press_key,
    computer_use_right_click,
    computer_use_scroll,
    computer_use_take_screenshot,
    computer_use_triple_click,
    computer_use_type,
    computer_use_wait,
)
from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from family_assistant.tools.browser_backend import JsonDict


class FakeBrowserBackend:
    """In-memory fake browser backend for testing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonDict]] = []
        self.current_url = "https://example.com"
        self.screen_width = 1280
        self.screen_height = 720

    async def screenshot_png(self) -> bytes:
        self.calls.append(("screenshot_png", {}))
        return b"\x89PNG\r\n\x1a\n"  # PNG header

    async def mouse_click(
        self, x: float, y: float, *, button: str = "left", click_count: int = 1
    ) -> None:
        self.calls.append((
            "mouse_click",
            {"x": x, "y": y, "button": button, "click_count": click_count},
        ))

    async def mouse_move(self, x: float, y: float) -> None:
        self.calls.append(("mouse_move", {"x": x, "y": y}))

    async def mouse_down(self) -> None:
        self.calls.append(("mouse_down", {}))

    async def mouse_up(self) -> None:
        self.calls.append(("mouse_up", {}))

    async def mouse_wheel(self, delta_x: float, delta_y: float) -> None:
        self.calls.append(("mouse_wheel", {"delta_x": delta_x, "delta_y": delta_y}))

    async def keyboard_type(self, text: str) -> None:
        self.calls.append(("keyboard_type", {"text": text}))

    async def keyboard_press(self, keys: str) -> None:
        self.calls.append(("keyboard_press", {"keys": keys}))

    async def keyboard_down(self, key: str) -> None:
        self.calls.append(("keyboard_down", {"key": key}))

    async def keyboard_up(self, key: str) -> None:
        self.calls.append(("keyboard_up", {"key": key}))

    async def goto(self, url: str) -> None:
        self.current_url = url
        self.calls.append(("goto", {"url": url}))

    async def go_back(self) -> None:
        self.calls.append(("go_back", {}))

    async def go_forward(self) -> None:
        self.calls.append(("go_forward", {}))

    async def raw_snapshot(self, next_ref: int) -> JsonDict:
        return {"url": self.current_url, "next_ref": next_ref}

    async def settle(self, timeout_ms: int = 5000) -> None:
        pass

    async def click(self, ref: str) -> None:
        pass

    async def fill(self, ref: str, text: str, submit: bool) -> None:
        pass

    async def select(self, ref: str, value: str) -> None:
        pass

    async def wait(self, selector: str | None, state: str, timeout_ms: int) -> None:
        pass

    async def extract_html(self, selector: str | None) -> str:
        return "<html></html>"

    async def evaluate(self, code: str) -> Any:  # noqa: ANN401 - JS evaluation returns arbitrary JSON, mirrors BrowserBackend protocol
        return None

    async def request_handoff(
        self,
        *,
        reason: str,
        handoff_note: str,
        expected_origin: str | None,
        allow_resume: bool = False,
    ) -> JsonDict:
        return {}

    async def claim_handback(self, session_id: str, handback_token: str) -> JsonDict:
        return {}

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_backend() -> FakeBrowserBackend:
    """Create a fake browser backend."""
    return FakeBrowserBackend()


@pytest.fixture
def exec_context(
    fake_backend: FakeBrowserBackend, monkeypatch: pytest.MonkeyPatch
) -> ToolExecutionContext:
    """Create a mock execution context with a fake backend."""

    # Patch get_browser_backend to return our fake backend
    async def mock_get_backend(ctx: ToolExecutionContext) -> FakeBrowserBackend:
        return fake_backend

    monkeypatch.setattr(cu_module, "get_browser_backend", mock_get_backend)

    ctx = MagicMock(spec=ToolExecutionContext)
    ctx.conversation_id = "test-conversation"
    # No batch: these tools are called one at a time, so the chokepoint only
    # has its lock to take.
    ctx.tool_call_batch = None
    ctx.tool_call_id = None
    return ctx


KEY_MAPPING_CASES = [
    ("a", "a"),
    ("Z", "Z"),
    ("1", "1"),
    ("ctrl", "Control"),
    ("control", "Control"),
    ("Ctrl", "Control"),
    ("CTRL", "Control"),
    ("meta", "Meta"),
    ("cmd", "Meta"),
    ("command", "Meta"),
    ("win", "Meta"),
    ("super", "Meta"),
    ("alt", "Alt"),
    ("option", "Alt"),
    ("enter", "Enter"),
    ("return", "Enter"),
    ("ENTER", "Enter"),
    ("esc", "Escape"),
    ("escape", "Escape"),
    ("Escape", "Escape"),
    ("up", "ArrowUp"),
    ("arrowup", "ArrowUp"),
    ("arrow_up", "ArrowUp"),
    ("down", "ArrowDown"),
    ("left", "ArrowLeft"),
    ("right", "ArrowRight"),
    ("pageup", "PageUp"),
    ("page_up", "PageUp"),
    ("pagedown", "PageDown"),
    ("page_down", "PageDown"),
    *[(f"f{i}", f"F{i}") for i in range(1, 13)],
]


class TestKeyMapping:
    """Test key name mapping from model names to Playwright names, observed via the public tool."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("model_key", "playwright_key"), KEY_MAPPING_CASES)
    async def test_press_key_maps_to_playwright_key(
        self,
        exec_context: ToolExecutionContext,
        fake_backend: FakeBrowserBackend,
        model_key: str,
        playwright_key: str,
    ) -> None:
        await computer_use_press_key(exec_context, model_key)
        press_calls = [c for c in fake_backend.calls if c[0] == "keyboard_press"]
        assert press_calls[-1][1]["keys"] == playwright_key


class TestClickActions:
    """Test click-related actions."""

    @pytest.mark.asyncio
    async def test_click(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        result = await computer_use_click(exec_context, 500, 350)
        assert isinstance(result, ToolResult)
        assert result.data == {"url": "https://example.com"}
        assert len(result.attachments or []) == 1
        if result.attachments:
            assert isinstance(result.attachments[0], ToolAttachment)
            assert result.attachments[0].mime_type == "image/png"

        # Check that mouse_click was called with correct denormalized coordinates
        calls = [c for c in fake_backend.calls if c[0] == "mouse_click"]
        assert len(calls) == 1
        assert calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert calls[0][1]["x"] == 640
        assert calls[0][1]["y"] == 251
        assert calls[0][1]["button"] == "left"
        assert calls[0][1]["click_count"] == 1

    @pytest.mark.asyncio
    async def test_double_click(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_double_click(exec_context, 500, 350)
        calls = [c for c in fake_backend.calls if c[0] == "mouse_click"]
        assert len(calls) == 1
        assert calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert calls[0][1]["click_count"] == 2

    @pytest.mark.asyncio
    async def test_triple_click(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_triple_click(exec_context, 500, 350)
        calls = [c for c in fake_backend.calls if c[0] == "mouse_click"]
        assert len(calls) == 1
        assert calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert calls[0][1]["click_count"] == 3

    @pytest.mark.asyncio
    async def test_middle_click(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_middle_click(exec_context, 500, 350)
        calls = [c for c in fake_backend.calls if c[0] == "mouse_click"]
        assert len(calls) == 1
        assert calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert calls[0][1]["button"] == "middle"

    @pytest.mark.asyncio
    async def test_right_click(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_right_click(exec_context, 500, 350)
        calls = [c for c in fake_backend.calls if c[0] == "mouse_click"]
        assert len(calls) == 1
        assert calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert calls[0][1]["button"] == "right"


class TestMouseActions:
    """Test mouse movement and button actions."""

    @pytest.mark.asyncio
    async def test_mouse_down(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_mouse_down(exec_context, 500, 350)
        move_calls = [c for c in fake_backend.calls if c[0] == "mouse_move"]
        down_calls = [c for c in fake_backend.calls if c[0] == "mouse_down"]
        assert len(move_calls) == 1
        assert move_calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert move_calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert len(down_calls) == 1

    @pytest.mark.asyncio
    async def test_mouse_up(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_mouse_up(exec_context, 500, 350)
        move_calls = [c for c in fake_backend.calls if c[0] == "mouse_move"]
        up_calls = [c for c in fake_backend.calls if c[0] == "mouse_up"]
        assert len(move_calls) == 1
        assert move_calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert move_calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert len(up_calls) == 1

    @pytest.mark.asyncio
    async def test_move(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_move(exec_context, 500, 350)
        move_calls = [c for c in fake_backend.calls if c[0] == "mouse_move"]
        assert len(move_calls) == 1
        assert move_calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert move_calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )


class TestKeyboardActions:
    """Test keyboard input actions."""

    @pytest.mark.asyncio
    async def test_type_replaces_field_contents(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        # The docs' handler for the native type action clears the focused
        # field (select-all + delete) before typing so corrected values
        # replace instead of concatenating.
        await computer_use_type(exec_context, "hello")
        actions = [
            (name, args)
            for name, args in fake_backend.calls
            if name in {"keyboard_type", "keyboard_press"}
        ]
        assert actions == [
            ("keyboard_press", {"keys": "Control+A"}),
            ("keyboard_press", {"keys": "Backspace"}),
            ("keyboard_type", {"text": "hello"}),
        ]

    @pytest.mark.asyncio
    async def test_type_with_enter(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_type(exec_context, "hello", press_enter=True)
        press_calls = [c for c in fake_backend.calls if c[0] == "keyboard_press"]
        assert press_calls[-1][1]["keys"] == "Enter"

    @pytest.mark.asyncio
    async def test_press_key(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_press_key(exec_context, "enter")
        press_calls = [c for c in fake_backend.calls if c[0] == "keyboard_press"]
        assert len(press_calls) == 1
        assert press_calls[0][1]["keys"] == "Enter"

    @pytest.mark.asyncio
    async def test_key_down(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_key_down(exec_context, "shift")
        down_calls = [c for c in fake_backend.calls if c[0] == "keyboard_down"]
        assert len(down_calls) == 1
        assert down_calls[0][1]["key"] == "Shift"

    @pytest.mark.asyncio
    async def test_key_up(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_key_up(exec_context, "shift")
        up_calls = [c for c in fake_backend.calls if c[0] == "keyboard_up"]
        assert len(up_calls) == 1
        assert up_calls[0][1]["key"] == "Shift"

    @pytest.mark.asyncio
    async def test_hotkey(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_hotkey(exec_context, ["ctrl", "c"])
        press_calls = [c for c in fake_backend.calls if c[0] == "keyboard_press"]
        assert len(press_calls) == 1
        assert press_calls[0][1]["keys"] == "Control+c"


class TestScrollAction:
    """Test scroll action."""

    @pytest.mark.asyncio
    async def test_scroll_down(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_scroll(exec_context, 500, 350, "down", 300)
        move_calls = [c for c in fake_backend.calls if c[0] == "mouse_move"]
        wheel_calls = [c for c in fake_backend.calls if c[0] == "mouse_wheel"]
        assert len(move_calls) == 1
        assert move_calls[0][1]["x"] == denormalize_coordinate(
            500, fake_backend.screen_width
        )
        assert move_calls[0][1]["y"] == denormalize_coordinate(
            350, fake_backend.screen_height
        )
        assert len(wheel_calls) == 1
        assert wheel_calls[0][1]["delta_y"] == 300

    @pytest.mark.asyncio
    async def test_scroll_up(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_scroll(exec_context, 500, 350, "up", 300)
        wheel_calls = [c for c in fake_backend.calls if c[0] == "mouse_wheel"]
        assert len(wheel_calls) == 1
        assert wheel_calls[0][1]["delta_y"] == -300

    @pytest.mark.asyncio
    async def test_scroll_left(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_scroll(exec_context, 500, 350, "left", 300)
        wheel_calls = [c for c in fake_backend.calls if c[0] == "mouse_wheel"]
        assert len(wheel_calls) == 1
        assert wheel_calls[0][1]["delta_x"] == -300

    @pytest.mark.asyncio
    async def test_scroll_right(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_scroll(exec_context, 500, 350, "right", 300)
        wheel_calls = [c for c in fake_backend.calls if c[0] == "mouse_wheel"]
        assert len(wheel_calls) == 1
        assert wheel_calls[0][1]["delta_x"] == 300

    @pytest.mark.asyncio
    async def test_scroll_invalid_direction(
        self, exec_context: ToolExecutionContext
    ) -> None:
        with pytest.raises(ValueError, match="Invalid scroll direction"):
            await computer_use_scroll(exec_context, 500, 350, "diagonal", 300)


class TestDragAndDrop:
    """Test drag and drop action."""

    @pytest.mark.asyncio
    async def test_drag_and_drop(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_drag_and_drop(exec_context, 100, 100, 500, 500)

        actions = [
            (name, args)
            for name, args in fake_backend.calls
            if name != "screenshot_png"
        ]
        action_names = [name for name, _ in actions]

        start_x = denormalize_coordinate(100, fake_backend.screen_width)
        start_y = denormalize_coordinate(100, fake_backend.screen_height)
        end_x = denormalize_coordinate(500, fake_backend.screen_width)
        end_y = denormalize_coordinate(500, fake_backend.screen_height)

        # move -> down -> (step moves) -> up, in that order
        assert action_names[0] == "mouse_move"
        assert action_names[1] == "mouse_down"
        assert action_names[-1] == "mouse_up"
        assert action_names.count("mouse_down") == 1
        assert action_names.count("mouse_up") == 1
        assert action_names.count("mouse_move") >= 10

        move_calls = [args for name, args in actions if name == "mouse_move"]
        assert move_calls[0]["x"] == start_x
        assert move_calls[0]["y"] == start_y
        assert move_calls[-1]["x"] == end_x
        assert move_calls[-1]["y"] == end_y


class TestNavigation:
    """Test navigation actions."""

    @pytest.mark.asyncio
    async def test_navigate(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_navigate(exec_context, "example.com")
        goto_calls = [c for c in fake_backend.calls if c[0] == "goto"]
        assert len(goto_calls) == 1
        assert goto_calls[0][1]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_navigate_with_protocol(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_navigate(exec_context, "https://example.com")
        goto_calls = [c for c in fake_backend.calls if c[0] == "goto"]
        assert len(goto_calls) == 1
        assert goto_calls[0][1]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_go_back(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_go_back(exec_context)
        back_calls = [c for c in fake_backend.calls if c[0] == "go_back"]
        assert len(back_calls) == 1

    @pytest.mark.asyncio
    async def test_go_forward(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        await computer_use_go_forward(exec_context)
        forward_calls = [c for c in fake_backend.calls if c[0] == "go_forward"]
        assert len(forward_calls) == 1


class TestScreenshot:
    """Test screenshot action."""

    @pytest.mark.asyncio
    async def test_take_screenshot(
        self, exec_context: ToolExecutionContext, fake_backend: FakeBrowserBackend
    ) -> None:
        result = await computer_use_take_screenshot(exec_context)
        assert isinstance(result, ToolResult)
        assert result.data == {"url": "https://example.com"}
        assert len(result.attachments or []) == 1


class TestWait:
    """Test wait action."""

    @pytest.mark.asyncio
    async def test_wait_default(self, exec_context: ToolExecutionContext) -> None:
        with patch.object(cu_module.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            await computer_use_wait(exec_context)
        sleep_mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_wait_clamped_zero(self, exec_context: ToolExecutionContext) -> None:
        with patch.object(cu_module.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            await computer_use_wait(exec_context, -10)
        sleep_mock.assert_awaited_once_with(0)

    @pytest.mark.asyncio
    async def test_wait_clamped_max(self, exec_context: ToolExecutionContext) -> None:
        with patch.object(cu_module.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            await computer_use_wait(exec_context, 100)
        sleep_mock.assert_awaited_once_with(30)


class TestToolDefinitions:
    """Test that tool definitions are properly structured."""

    def test_definition_names_match_function_names(self) -> None:
        """Test that COMPUTER_USE_TOOLS_DEFINITION names match COMPUTER_USE_FUNCTION_NAMES."""
        defined_names = {
            tool["function"]["name"] for tool in cu_module.COMPUTER_USE_TOOLS_DEFINITION
        }
        assert defined_names == COMPUTER_USE_FUNCTION_NAMES

    def test_all_definitions_have_required_fields(self) -> None:
        """Test that all tool definitions have required fields."""
        for tool in cu_module.COMPUTER_USE_TOOLS_DEFINITION:
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]
