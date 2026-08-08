"""The disconnect middleware stops abandoned safe requests, and only those."""

import asyncio
import contextlib

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
async def test_disconnect_after_the_response_is_not_treated_as_abandonment() -> None:
    """Post-response work survives the disconnect the server always reports.

    uvicorn returns ``http.disconnect`` from ``receive()`` once the response is
    complete, whether or not the client went anywhere. Acting on that would
    cancel background tasks and ``yield``-dependency teardown on healthy
    requests.
    """
    response_sent = asyncio.Event()
    resume = asyncio.Event()
    after_response = asyncio.Event()
    cancelled = asyncio.Event()

    class _WorksAfterResponding:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await send(
                {"type": "http.response.start", "status": 200, "headers": []},
            )
            await send({"type": "http.response.body", "body": b"done"})
            # Stand-in for a BackgroundTask or a yield-dependency teardown:
            # work the handler still owes after its last body chunk.
            try:
                await resume.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            after_response.set()

    async def receive() -> Message:
        # Exactly what uvicorn does: report a disconnect once the response is
        # complete, whether or not the client actually went away.
        await response_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and not message.get(
            "more_body", False
        ):
            response_sent.set()

    middleware = CancelOnClientDisconnectMiddleware(_WorksAfterResponding())
    task = asyncio.create_task(middleware(_scope("GET"), receive, send))

    await response_sent.wait()
    # Bounded negative wait: if the middleware is going to act on that
    # disconnect it does so as soon as it reads it, far inside this window.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    assert cancelled.is_set() is False, "post-response work was cancelled"

    resume.set()
    await asyncio.wait_for(task, timeout=5)

    assert after_response.is_set() is True


@pytest.mark.asyncio
async def test_state_changing_get_routes_are_exempt() -> None:
    """The OAuth callback consumes a single-use flow, so it must not be cut off."""
    spy = _HandlerSpy()

    scope = _scope("GET")
    scope["path"] = "/api/integrations/google/callback"

    async def receive() -> Message:
        # The client is already gone before the handler gets anywhere.
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pass

    middleware = CancelOnClientDisconnectMiddleware(spy)
    task = asyncio.create_task(middleware(scope, receive, send))

    await spy.started.wait()
    # Bounded negative wait: an unexempted path is cancelled as soon as the
    # middleware reads that disconnect, well inside this window.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    assert spy.cancelled is False, "the OAuth callback was cut off mid-flight"

    spy.release.set()
    await asyncio.wait_for(task, timeout=5)

    assert spy.completed is True


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

    # GET, so the request actually goes through the queue. A POST would take
    # the pass-through branch and prove nothing about the forwarding.
    await asyncio.wait_for(middleware(_scope("GET"), receive, send), timeout=5)

    assert [m["body"] for m in seen] == [b"part-1", b"part-2"]
