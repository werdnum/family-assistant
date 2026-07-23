"""Unit tests for the local BrowserSession timezone plumbing.

The remote browser-server backend forwards the profile's configured timezone so
in-page ``new Date()`` reports local time; these tests cover the parity behaviour
for the local Playwright session (``get_browser_session`` -> ``BrowserSession``).
The real browser launch is exercised by ``tests/functional/tools/test_browser_dom.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.tools.browser_session import (
    close_browser_session,
    get_browser_session,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


def _ctx(conversation_id: str, timezone: ZoneInfo | None) -> ToolExecutionContext:
    ctx = SimpleNamespace(conversation_id=conversation_id, timezone=timezone)
    return cast("ToolExecutionContext", cast("object", ctx))


@pytest.mark.asyncio
async def test_get_browser_session_sets_timezone_from_context() -> None:
    ctx = _ctx("conv_local_tz", ZoneInfo("Australia/Sydney"))
    session = await get_browser_session(ctx)
    try:
        assert session.timezone_id == "Australia/Sydney"
    finally:
        await close_browser_session(ctx)


@pytest.mark.asyncio
async def test_get_browser_session_timezone_none_when_absent() -> None:
    ctx = _ctx("conv_local_no_tz", None)
    session = await get_browser_session(ctx)
    try:
        assert session.timezone_id is None
    finally:
        await close_browser_session(ctx)
