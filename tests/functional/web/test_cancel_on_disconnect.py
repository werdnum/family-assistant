"""The disconnect middleware stops abandoned safe requests, and only those."""

import asyncio

import pytest
from starlette.types import Message, Receive, Scope, Send

from family_assistant.web.cancel_on_disconnect import (
    CancelOnClientDisconnectMiddleware,
)


class _HandlerSpy:
    """An ASGI app that blocks until released, recording whether it finished."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = False
        self.cancelled = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.completed = True
        await send(
            {"type": "http.response.start", "status": 200, "headers": []},
        )
        await send({"type": "http.response.body", "body": b"done"})


def _scope(method: str) -> Scope:
    return {
        "type": "http",
        "method": method,
        "path": "/api/v1/chat/conversations",
        "headers": [],
    }


async def _run(method: str, spy: _HandlerSpy, messages: list[Message]) -> list[Message]:
    """Drive the middleware, feeding ``messages`` as the client's input."""
    sent: list[Message] = []
    pending = list(messages)

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        # Nothing further from this client; block rather than signalling EOF.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = CancelOnClientDisconnectMiddleware(spy)
    await asyncio.wait_for(middleware(_scope(method), receive, send), timeout=5)
    return sent


@pytest.mark.asyncio
async def test_get_is_cancelled_when_client_disconnects() -> None:
    """An abandoned GET stops instead of running to completion."""
    spy = _HandlerSpy()

    sent = await _run("GET", spy, [{"type": "http.disconnect"}])

    assert spy.cancelled is True
    assert spy.completed is False
    assert sent == []


@pytest.mark.asyncio
async def test_post_runs_to_completion_after_disconnect() -> None:
    """A state-changing request survives its client leaving.

    A chat turn is expected to outlive the request that started it -- the
    client reconnects and follows the reservation to the reply -- so
    cancelling on disconnect would drop the turn.
    """
    spy = _HandlerSpy()

    async def release_once_started() -> None:
        await spy.started.wait()
        spy.release.set()

    releaser = asyncio.create_task(release_once_started())
    sent = await _run("POST", spy, [{"type": "http.disconnect"}])
    await releaser

    assert spy.completed is True
    assert spy.cancelled is False
    assert sent[0]["type"] == "http.response.start"


@pytest.mark.asyncio
async def test_connected_get_completes_normally() -> None:
    """Without a disconnect the middleware is transparent."""
    spy = _HandlerSpy()

    async def release_once_started() -> None:
        await spy.started.wait()
        spy.release.set()

    releaser = asyncio.create_task(release_once_started())
    sent = await _run("GET", spy, [])
    await releaser

    assert spy.completed is True
    assert spy.cancelled is False
    assert [m["type"] for m in sent] == [
        "http.response.start",
        "http.response.body",
    ]


@pytest.mark.asyncio
async def test_handler_exceptions_propagate_unchanged() -> None:
    """The middleware must not swallow a handler's own failure."""

    class _Failing:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            raise RuntimeError("handler blew up")

    middleware = CancelOnClientDisconnectMiddleware(_Failing())

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        pass

    with pytest.raises(RuntimeError, match="handler blew up"):
        await asyncio.wait_for(middleware(_scope("GET"), receive, send), timeout=5)


@pytest.mark.asyncio
async def test_cancelling_the_middleware_does_not_orphan_the_handler() -> None:
    """A server shutdown must take the handler down with it.

    Leaving it running detached would recreate the orphan this middleware
    exists to prevent, just by a different route.
    """
    spy = _HandlerSpy()

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        pass

    middleware = CancelOnClientDisconnectMiddleware(spy)
    outer = asyncio.create_task(middleware(_scope("GET"), receive, send))
    await spy.started.wait()

    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer

    assert spy.cancelled is True
    assert spy.completed is False


@pytest.mark.asyncio
async def test_body_still_reaches_the_handler() -> None:
    """Consuming ``receive`` to watch for disconnects must not eat the body."""
    seen: list[Message] = []

    class _BodyReader:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            while True:
                message = await receive()
                seen.append(message)
                if not message.get("more_body", False):
                    return

    middleware = CancelOnClientDisconnectMiddleware(_BodyReader())
    pending: list[Message] = [
        {"type": "http.request", "body": b"part-1", "more_body": True},
        {"type": "http.request", "body": b"part-2", "more_body": False},
    ]

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        pass

    # POST so the middleware passes through; the body path is method-independent
    # but this is the shape that actually carries one.
    await asyncio.wait_for(middleware(_scope("POST"), receive, send), timeout=5)

    assert [m["body"] for m in seen] == [b"part-1", b"part-2"]
