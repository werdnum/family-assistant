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

import contextlib
import contextvars
import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Generator

    from family_assistant.llm.model_selection import ResolvedModelSelection

__all__ = [
    "CallAttribution",
    "ModelSelectionToken",
    "ProfileToken",
    "ServiceTier",
    "Workload",
    "attributed_call",
    "attributed_to_profile",
    "current_model_selection",
    "current_processing_profile",
    "current_service_tier",
    "current_workload",
    "reset_model_selection",
    "reset_processing_profile",
    "running_as",
    "set_model_selection",
    "set_processing_profile",
    "with_service_tier",
]


class Workload(enum.Enum):
    """Whether anybody is waiting for the answer to the work in progress.

    Orthogonal to the model tier: background work may still need the deep
    tier, it just does not need capacity immediately.
    """

    INTERACTIVE = "interactive"
    BACKGROUND = "background"


type ServiceTier = Literal["standard", "flex"]
"""The price class a provider request is sent at."""

_profile: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_processing_profile", default=None
)

_model_selection: contextvars.ContextVar[ResolvedModelSelection | None] = (
    contextvars.ContextVar("llm_model_selection", default=None)
)

# Interactive unless the code that started the work said otherwise: work that
# nobody marked then runs at full price, rather than making a person wait.
_workload: contextvars.ContextVar[Workload] = contextvars.ContextVar(
    "llm_workload", default=Workload.INTERACTIVE
)

# Set only around a single provider attempt by the Flex wrapper, so it never
# outlives the attempt it was set for (see ``with_service_tier``).
_service_tier: contextvars.ContextVar[ServiceTier] = contextvars.ContextVar(
    "llm_service_tier", default="standard"
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
    # Which price class the call was sent at. Read from the in-flight attempt,
    # never established by ``attributed_call``: the tier is a property of one
    # provider attempt, not of the turn.
    service_tier: ServiceTier = "standard"


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


@contextlib.contextmanager
def attributed_call(attribution: CallAttribution) -> Generator[None]:
    """Attribute the calls made inside this block to *attribution*.

    For a plain awaited call, where :func:`attributed_to_profile`'s reason for
    not being a context manager does not apply: nothing yields out of the
    block, so the reset always runs in the context that set it.
    """
    profile_token = set_processing_profile(attribution.profile_id)
    selection_token = set_model_selection(attribution.model_selection)
    try:
        yield
    finally:
        reset_model_selection(selection_token)
        reset_processing_profile(profile_token)


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


def current_workload() -> Workload:
    """Whether the work in progress has somebody waiting on it."""
    return _workload.get()


@contextlib.contextmanager
def running_as(workload: Workload) -> Generator[None]:
    """Run the block, and every LLM call it makes, as *workload*.

    For the code that starts a unit of work -- the task worker around a
    handler -- so everything underneath inherits it without an argument.
    """
    token = _workload.set(workload)
    try:
        yield
    finally:
        _workload.reset(token)


def current_service_tier() -> ServiceTier:
    """The price class the provider request being built should be sent at."""
    return _service_tier.get()


@contextlib.contextmanager
def with_service_tier(tier: ServiceTier) -> Generator[None]:
    """Send the provider requests made inside this block at *tier*.

    Callers bracket each awaited step of an attempt, never a ``yield``, for the
    reason :func:`attributed_to_profile` gives.
    """
    token = _service_tier.set(tier)
    try:
        yield
    finally:
        _service_tier.reset(token)
