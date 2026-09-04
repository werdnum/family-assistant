"""Which processing profile the in-flight LLM call belongs to.

The provider clients sit below the processing layer and know nothing about
profiles, but "which profile is spending the tokens" is the first question
anyone asks of an LLM bill. Threading a profile argument down through every
client constructor and every call site would put the answer in as many places
as there are providers, and a provider added later would silently lose it.

A ContextVar instead: the streaming loop sets it once per turn, and the
telemetry object at the bottom reads it. Code running outside a turn -- a
one-shot helper, an eval runner -- reads ``None`` and is labelled as
unattributed rather than dropped.

Set and reset are separate functions rather than a context manager because the
only caller is an async generator, where a ``with`` block wrapped around a
``yield`` is exactly the shape that may not run its cleanup --
:func:`attributed_to_profile` exists so no caller has to get that right twice.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

__all__ = [
    "ProfileToken",
    "attributed_to_profile",
    "current_processing_profile",
    "reset_processing_profile",
    "set_processing_profile",
]

_profile: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_processing_profile", default=None
)

type ProfileToken = contextvars.Token[str | None]


def current_processing_profile() -> str | None:
    """The profile whose turn is running here, or ``None`` outside a turn."""
    return _profile.get()


def set_processing_profile(profile_id: str) -> ProfileToken:
    """Attribute LLM calls made from here on to *profile_id*."""
    return _profile.set(profile_id)


def reset_processing_profile(token: ProfileToken) -> None:
    """Undo the attribution *token* established."""
    _profile.reset(token)


async def attributed_to_profile[T](
    profile_id: str,
    source: AsyncIterator[T],
) -> AsyncGenerator[T]:
    """Re-yield *source*, attributing only its own execution to *profile_id*.

    The attribution is established around each pull and released before the
    item goes out, never held across a ``yield``. Two things go wrong if it is
    held: a consumer that leaves its ``async for`` with ``break`` never closes
    the generator, so the finalizer closes it later in a Context of its own,
    where resetting a token created elsewhere raises ``ValueError``; and until
    that happens the profile stays set in the consumer's own context, where it
    misattributes whatever that task does next.

    Bracketing the pull also scopes the attribution to exactly the code that
    makes the LLM call, rather than to the consumer's handling of each event.
    """
    while True:
        token = set_processing_profile(profile_id)
        try:
            item = await anext(source)
        except StopAsyncIteration:
            return
        finally:
            reset_processing_profile(token)
        yield item
