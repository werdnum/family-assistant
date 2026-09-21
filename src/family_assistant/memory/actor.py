"""Who wrote a memory change.

Kept apart from the edit list because the same list means different things
depending on who proposed it: a curator review, a foreground "remember this",
or a person editing in the notes UI. The recent-changes view shows the
distinction, and the evidence differs with it -- a curator cites the reviewed
transcript, a person's edit carries only the authenticated editor and the time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemoryActorKind(StrEnum):
    """The three kinds of writer v1 distinguishes."""

    CURATOR = "curator"
    ASSISTANT = "assistant"
    PERSON = "person"


@dataclass(frozen=True)
class MemoryActor:
    """The writer of one apply, as the change log records it."""

    kind: MemoryActorKind
    identity: str | None = None
    """Who, where it is known: a user id, or the profile a review ran under."""
    interface_type: str | None = None
    conversation_id: str | None = None
