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

Two exceptions to "GET is safe", both of which this module has to encode
because this application does not honour that part of HTTP everywhere:

* A GET that has already written is no longer safe to drop, so the decision
  waits until the disconnect arrives and asks what the request has actually
  done (``request_side_effects``). The OAuth integration callback is the case
  that matters: it single-use-claims the pending flow and only then redeems
  the authorization code over the network, so cancelling in between consumes
  both and stores nothing -- but the claim is a write, so by that point the
  request has stopped being cancellable. Deciding from observed writes rather
  than from a list of paths means a route added later is covered without
  anyone remembering to add it.

  Writes are the only side effect visible this way. ``EXEMPT_PATHS`` covers the
  routes whose side effect is something else: the OIDC and app-auth flows
  redeem a one-time credential with the provider and then record the result in
  the session cookie or in process memory, so nothing marks them. The app-auth
  callback is the one that hurts -- it is how the iOS client logs in, and the
  provider's code is spent whether or not the handler survives to mint the
  app's own. A test walks the built application and fails if a session-mutating
  GET route is missing from that list.
* ``receive()`` reports ``http.disconnect`` once the response is complete,
  whether or not the client actually went away (uvicorn returns it
  unconditionally on ``response_complete``). Treating that as a disconnect
  would cancel whatever the handler still had to do after its last body chunk
  -- background tasks, ``yield``-dependency teardown -- on perfectly healthy
  requests. So a disconnect only counts while the response is still unfinished,
  which still covers a streaming response abandoned mid-flight.
"""

import asyncio
import contextlib
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from family_assistant.request_side_effects import begin_tracking

logger = logging.getLogger(__name__)

# HTTP's safe methods: no side effect worth preserving once the client is gone.
CANCELLABLE_METHODS = frozenset({"GET", "HEAD"})

# GET routes whose side effect is something the write tracker cannot see. All of
# them redeem a one-time credential with the OIDC provider and then record the
# result in the session cookie or in process memory, so no database write ever
# reports them -- and the provider's code is spent either way, which is what
# makes cancelling one unrecoverable rather than merely retryable.
#
# ``test_get_routes_with_untracked_side_effects_are_exempt`` walks the built
# application and fails if a GET route mutates the session without appearing
# here, so this list is checked rather than remembered. That check reads each
# endpoint's own source, so a route that mutates the session inside a helper it
# calls -- as the ``/auth`` family does -- still has to be added by hand.
EXEMPT_PATHS = frozenset({
    "/login",
    "/logout",
    "/auth",
    "/app-auth",
    "/app-auth-callback",
})


def _is_cancellable(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") not in CANCELLABLE_METHODS:
        return False
    return scope.get("path", "") not in EXEMPT_PATHS


class CancelOnClientDisconnectMiddleware:
    """Cancel in-flight safe-method requests when the client disconnects."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_cancellable(scope):
            await self.app(scope, receive, send)
            return

        # Installed here, before the handler's task exists, so that task
        # inherits this same tracker and its writes are visible below.
        side_effects = begin_tracking()

        # Set before the message reaches the server, so this can never lag
        # uvicorn's own ``response_complete`` and mistake the disconnect it
        # then synthesizes for a real one.
        response_complete = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_complete
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete = True
            await send(message)

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
                    # Only actionable while there is still a response to
                    # deliver; afterwards this is just the server reporting
                    # that the exchange is over.
                    if not response_complete:
                        disconnected.set()
                    return

        async def receive_from_queue() -> Message:
            return await incoming.get()

        async def run_handler() -> None:
            await self.app(scope, receive_from_queue, tracked_send)

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

            if side_effects.state_changed:
                # It has written something. Finishing costs a little database
                # time; abandoning it half-done could cost the write's whole
                # point, so let it run.
                logger.info(
                    "Client disconnected from %s %s, but it has already written; "
                    "letting it finish",
                    scope.get("method"),
                    scope.get("path"),
                )
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
