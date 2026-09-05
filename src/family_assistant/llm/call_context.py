"""Which profile and which model tier the in-flight LLM call belongs to.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from family_assistant.processing.model_selection import ResolvedModelSelection

__all__ = [
    "CallAttribution",
    "ModelSelectionToken",
    "ProfileToken",
    "attributed_to_profile",
    "current_model_selection",
    "current_processing_profile",
    "reset_model_selection",
    "reset_processing_profile",
    "set_model_selection",
    "set_processing_profile",
]

_profile: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_processing_profile", default=None
)

_model_selection: contextvars.ContextVar[ResolvedModelSelection | None] = (
    contextvars.ContextVar("llm_model_selection", default=None)
)

type ProfileToken = contextvars.Token[str | None]
type ModelSelectionToken = contextvars.Token[ResolvedModelSelection | None]


@dataclass(frozen=True)
class CallAttribution:
    """Everything a call below the processing layer is attributed to.

    One object so a new dimension is added to the wrapper once rather than to
    every caller's argument list.
    """

    profile_id: str
    model_selection: ResolvedModelSelection | None = None


def current_processing_profile() -> str | None:
    """The profile whose turn is running here, or ``None`` outside a turn."""
    return _profile.get()


def set_processing_profile(profile_id: str) -> ProfileToken:
    """Attribute LLM calls made from here on to *profile_id*."""
    return _profile.set(profile_id)


def reset_processing_profile(token: ProfileToken) -> None:
    """Undo the attribution *token* established."""
    _profile.reset(token)


def current_model_selection() -> ResolvedModelSelection | None:
    """The tier selection the running turn was frozen to, if any."""
    return _model_selection.get()


def set_model_selection(
    selection: ResolvedModelSelection | None,
) -> ModelSelectionToken:
    """Attribute LLM calls made from here on to *selection*."""
    return _model_selection.set(selection)


def reset_model_selection(token: ModelSelectionToken) -> None:
    """Undo the attribution *token* established."""
    _model_selection.reset(token)


async def attributed_to_profile[T](
    attribution: CallAttribution,
    source: AsyncIterator[T],
) -> AsyncGenerator[T]:
    """Re-yield *source*, attributing only its own execution to *attribution*.

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
        profile_token = set_processing_profile(attribution.profile_id)
        selection_token = set_model_selection(attribution.model_selection)
        try:
            item = await anext(source)
        except StopAsyncIteration:
            return
        finally:
            reset_model_selection(selection_token)
            reset_processing_profile(profile_token)
        yield item
