"""Unit tests for the ordering machinery behind concurrent browser tool calls.

A model response's tool calls run concurrently, but every browser operation for
one conversation has to run in the order the model issued it, each against the
page the previous one left. :class:`ToolCallBatch` carries that order and
:meth:`BrowserSession.operation` enforces it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from family_assistant.tools import AVAILABLE_FUNCTIONS
from family_assistant.tools.browser_session import BROWSER_TOOL_NAMES, BrowserSession
from family_assistant.tools.types import ToolCallBatch, ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Coroutine

pytestmark = pytest.mark.asyncio


def _batch() -> ToolCallBatch:
    return ToolCallBatch([
        ("call_1", "browser_click"),
        ("call_2", "browser_fill"),
        ("call_3", "get_note"),
    ])


def _context(batch: ToolCallBatch | None, call_id: str | None) -> ToolExecutionContext:
    return MagicMock(
        spec=ToolExecutionContext,
        conversation_id="ordering-test",
        tool_call_batch=batch,
        tool_call_id=call_id,
    )


class TestToolCallBatch:
    """The issue order of one response's tool calls."""

    def test_earlier_lists_only_preceding_calls(self) -> None:
        assert _batch().earlier("call_2") == [("call_1", "browser_click")]

    def test_earlier_is_empty_for_the_first_call(self) -> None:
        assert _batch().earlier("call_1") == []

    def test_later_lists_only_following_calls(self) -> None:
        assert _batch().later("call_1") == [
            ("call_2", "browser_fill"),
            ("call_3", "get_note"),
        ]

    def test_later_is_empty_for_the_last_call(self) -> None:
        assert _batch().later("call_3") == []

    def test_an_unknown_call_id_is_an_error_rather_than_an_empty_list(self) -> None:
        with pytest.raises(ValueError, match="not part of this tool-call batch"):
            _batch().earlier("call_nope")

    def test_marking_an_unknown_call_done_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="not part of this tool-call batch"):
            _batch().mark_done("call_nope")

    async def test_wait_done_returns_once_every_named_call_is_done(self) -> None:
        batch = _batch()
        waiter = asyncio.create_task(batch.wait_done(["call_1", "call_2"]))
        batch.mark_done("call_1")
        assert not waiter.done()
        batch.mark_done("call_2")
        await asyncio.wait_for(waiter, timeout=1)

    async def test_wait_done_returns_immediately_for_no_calls(self) -> None:
        await asyncio.wait_for(_batch().wait_done([]), timeout=1)


@dataclass
class _Recorder:
    """Records the order in which operations entered and left the chokepoint."""

    entered: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)

    async def run(
        self, session: BrowserSession, context: ToolExecutionContext, name: str
    ) -> None:
        async with session.operation(context):
            self.entered.append(name)
            self.left.append(name)


def _reporting(
    batch: ToolCallBatch, call_id: str, body: Coroutine[None, None, None]
) -> Coroutine[None, None, None]:
    """Wrap ``body`` the way the executor does: report completion regardless."""

    async def _run() -> None:
        try:
            await body
        finally:
            batch.mark_done(call_id)

    return _run()


class TestBrowserSessionOperation:
    """The per-conversation chokepoint every browser operation runs through."""

    async def test_runs_batch_siblings_in_issue_order_whatever_order_they_start(
        self,
    ) -> None:
        session = BrowserSession()
        batch = _batch()
        recorder = _Recorder()
        # The second-issued call is started first; it must still run second.
        second = asyncio.create_task(
            _reporting(
                batch, "call_2", recorder.run(session, _context(batch, "call_2"), "b")
            )
        )
        first = asyncio.create_task(
            _reporting(
                batch, "call_1", recorder.run(session, _context(batch, "call_1"), "a")
            )
        )
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        assert recorder.entered == ["a", "b"]

    async def test_does_not_wait_for_non_browser_siblings(self) -> None:
        """A note lookup issued first must not hold up a browser action."""
        session = BrowserSession()
        batch = ToolCallBatch([
            ("call_note", "get_note"),
            ("call_click", "browser_click"),
        ])
        recorder = _Recorder()
        # ``call_note`` never reports done; the browser call runs anyway.
        await asyncio.wait_for(
            recorder.run(session, _context(batch, "call_click"), "click"), timeout=2
        )
        assert recorder.entered == ["click"]

    async def test_serialises_operations_without_a_batch(self) -> None:
        session = BrowserSession()
        recorder = _Recorder()
        await asyncio.wait_for(
            asyncio.gather(
                recorder.run(session, _context(None, None), "a"),
                recorder.run(session, _context(None, None), "b"),
            ),
            timeout=2,
        )
        # The lock means one operation completes before the next begins.
        assert recorder.entered == recorder.left

    async def test_a_sibling_that_never_ran_does_not_wedge_the_batch(self) -> None:
        """Denials and failures report completion, so the rest of the batch runs."""
        session = BrowserSession()
        batch = _batch()
        recorder = _Recorder()
        waiting = asyncio.create_task(
            recorder.run(session, _context(batch, "call_2"), "b")
        )
        # ``call_1`` was denied before it reached the browser.
        batch.mark_done("call_1")
        await asyncio.wait_for(waiting, timeout=2)
        assert recorder.entered == ["b"]


class TestBrowserToolRegistry:
    """Every browser tool registers itself via ``@browser_operation``."""

    def test_registry_matches_the_tool_table(self) -> None:
        # A batch carries the names tools are exposed under, so the registry
        # must hold exactly those names for every decorated implementation:
        # a name that differs (or a browser tool without the decorator) would
        # silently drop that tool out of issue-order waiting.
        decorated = {
            name
            for name, implementation in AVAILABLE_FUNCTIONS.items()
            if getattr(implementation, "browser_tool_name", None) is not None
        }
        assert decorated == BROWSER_TOOL_NAMES
        assert {"browser_click", "click", "navigate", "take_screenshot"} <= decorated
        for name in decorated:
            assert AVAILABLE_FUNCTIONS[name].browser_tool_name == name
