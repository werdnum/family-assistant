"""Track whether the in-flight request has changed anything yet.

Abandoning a request that has only read is free; abandoning one that has
already written is not, and the difference cannot be read off the route. The
OAuth integration callback is the case that forced this: it is a GET, so by
HTTP's rules it is safe to drop, but it single-use-claims the pending flow and
only then redeems the authorization code over the network. Cancelled in
between, it consumes both and stores nothing.

Enumerating such routes by path is a list that goes stale the first time
someone adds one. Instead the storage layer reports the write as it happens
(:func:`mark_state_changed`), and whoever is deciding whether to abandon the
request asks what has happened so far (:func:`state_changed`).

The tracker is a mutable object behind a ``ContextVar`` on purpose. A
ContextVar is copied into a task at creation, not shared with it, so a flag set
inside the request's task would be invisible to the caller that spawned it.
Sharing one object means the write is visible to both.

Scope and limits:

* Only database writes are reported. An outbound HTTP call, a session cookie,
  or a file write is invisible here, so a route whose only side effect is one of
  those still needs handling of its own.
* A write inside a transaction that later rolls back still counts. Marking on
  execute rather than on commit errs towards keeping the request alive, which
  is the safe direction.
* Code running outside a request -- the task worker, startup -- has no tracker
  installed, so reporting a write there does nothing.
"""

import contextvars
from dataclasses import dataclass, field


@dataclass
class RequestSideEffects:
    """What the current request has done that outliving it would matter for."""

    state_changed: bool = field(default=False)


_side_effects: contextvars.ContextVar[RequestSideEffects | None] = (
    contextvars.ContextVar("request_side_effects", default=None)
)


def begin_tracking() -> RequestSideEffects:
    """Install a fresh tracker for this context and return it.

    Call before spawning the task that handles the request, so that task
    inherits this same object.
    """
    tracker = RequestSideEffects()
    _side_effects.set(tracker)
    return tracker


def mark_state_changed() -> None:
    """Report that the request has changed durable state.

    A no-op outside a tracked request.
    """
    tracker = _side_effects.get()
    if tracker is not None:
        tracker.state_changed = True


def state_changed() -> bool:
    """Whether the request has changed durable state so far."""
    tracker = _side_effects.get()
    return tracker is not None and tracker.state_changed
