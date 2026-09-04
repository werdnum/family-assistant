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
``yield`` is exactly the shape that may not run its cleanup.
"""

from __future__ import annotations

import contextvars

__all__ = [
    "ProfileToken",
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
