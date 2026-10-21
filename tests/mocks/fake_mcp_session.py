"""In-memory stand-ins for ``mcp.ClientSession`` in MCP provider unit tests.

The provider only asks a session to list its tools and, when the session
offers it, to answer a ping. These fakes answer from memory and count the
calls, so tests can check which probe the health check used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mcp.types import EmptyResult, ListToolsResult, Tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mcp import ClientSession


class FakeListToolsSession:
    """A session that lists a fixed tool set and offers no ``send_ping``.

    ``list_tools_error`` is raised by every listing while it is set.
    ``pages`` replaces ``tools`` with a paginated listing, one page per cursor.
    """

    def __init__(
        self,
        tools: Sequence[Tool] = (),
        *,
        list_tools_error: BaseException | None = None,
        pages: Sequence[Sequence[Tool]] | None = None,
    ) -> None:
        self.tools = list(tools)
        self.list_tools_error = list_tools_error
        self._pages = [list(page) for page in pages] if pages is not None else None
        self.list_tools_cursors: list[str | None] = []

    @property
    def list_tools_calls(self) -> int:
        return len(self.list_tools_cursors)

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        self.list_tools_cursors.append(cursor)
        if self.list_tools_error is not None:
            raise self.list_tools_error
        if self._pages is None:
            return ListToolsResult(tools=list(self.tools))
        index = int(cursor) if cursor else 0
        next_cursor = str(index + 1) if index + 1 < len(self._pages) else None
        return ListToolsResult(tools=self._pages[index], nextCursor=next_cursor)

    def as_client_session(self) -> ClientSession:
        return cast("ClientSession", self)


class FakeMcpSession(FakeListToolsSession):
    """A session that also answers ``send_ping``.

    ``ping_error`` is raised by every ping while it is set; ``on_ping`` runs
    before each ping is answered, for tests that act while a ping is in flight.
    """

    def __init__(
        self,
        tools: Sequence[Tool] = (),
        *,
        ping_error: BaseException | None = None,
        list_tools_error: BaseException | None = None,
        on_ping: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(tools, list_tools_error=list_tools_error)
        self.ping_error = ping_error
        self.on_ping = on_ping
        self.ping_calls = 0

    async def send_ping(self) -> EmptyResult:
        self.ping_calls += 1
        if self.on_ping is not None:
            await self.on_ping()
        if self.ping_error is not None:
            raise self.ping_error
        return EmptyResult()
