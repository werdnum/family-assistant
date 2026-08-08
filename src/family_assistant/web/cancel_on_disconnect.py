"""Stop serving a request whose client has already gone away.

ASGI hands the application a ``http.disconnect`` message when the client's
connection drops, but nothing consumes it unless the handler happens to poll
for it, so by default a handler runs to completion for a client that will never
see the response. The work is not free: it holds a pooled database connection
and keeps the query it issued running on the server. A cancelled
conversation-list request left its query running for 41 minutes after the
client had disconnected 2.9 seconds in, and the client's own retries then
queued behind the capacity its abandoned predecessors were still consuming --
each retry adding another orphan.

Cancelling the handler is what stops the database work: asyncpg issues a
server-side cancel for the statement it is awaiting when its task is cancelled,
so the query dies with the request rather than outliving it.

Only safe methods are cancelled. A GET or HEAD whose client is gone has nothing
left to deliver, so abandoning it loses nothing. Anything that can change state
runs to completion deliberately: a chat turn is expected to outlive the request
that started it -- the client reconnects and follows the reservation to the
reply -- and cancelling it mid-flight would drop the turn instead.
"""

import asyncio
import contextlib
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# HTTP's safe methods: no side effect worth preserving once the client is gone.
CANCELLABLE_METHODS = frozenset({"GET", "HEAD"})


class CancelOnClientDisconnectMiddleware:
    """Cancel in-flight safe-method requests when the client disconnects."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in CANCELLABLE_METHODS:
            await self.app(scope, receive, send)
            return

        # The handler reads from this queue rather than from ``receive``
        # directly, so the watcher below can consume ``receive`` continuously
        # and still deliver every message the handler is entitled to. Polling
        # ``receive`` only when the handler asks would never see the disconnect
        # of a handler that is busy in the database and not reading its body.
        incoming: asyncio.Queue[Message] = asyncio.Queue()
        disconnected = asyncio.Event()

        async def watch_for_disconnect() -> None:
            while True:
                message = await receive()
                await incoming.put(message)
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return

        async def receive_from_queue() -> Message:
            return await incoming.get()

        async def run_handler() -> None:
            await self.app(scope, receive_from_queue, send)

        watcher = asyncio.create_task(watch_for_disconnect())
        handler = asyncio.create_task(run_handler())
        disconnect = asyncio.create_task(disconnected.wait())

        try:
            done, _pending = await asyncio.wait(
                {handler, disconnect}, return_when=asyncio.FIRST_COMPLETED
            )
            if handler in done:
                # Re-raise whatever the handler raised, exactly as if this
                # middleware were not here.
                await handler
                return

            handler.cancel()
            # Expected: this middleware asked for it. Swallowing keeps the
            # cancellation from reading as a shutdown to the server above.
            with contextlib.suppress(asyncio.CancelledError):
                await handler
            logger.info(
                "Cancelled %s %s: client disconnected before the response was sent",
                scope.get("method"),
                scope.get("path"),
            )
        finally:
            # ``handler`` is already finished on both paths above, but not when
            # this middleware's own task is cancelled -- a server shutdown, say.
            # Leaving it to run detached there would recreate exactly the orphan
            # this class exists to prevent.
            for task in (handler, watcher, disconnect):
                task.cancel()
            await asyncio.gather(handler, watcher, disconnect, return_exceptions=True)
