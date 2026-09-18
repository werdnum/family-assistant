"""The timing and scope parameters the memory review sweep runs under.

A value rather than a config model, for the same reason
:class:`~family_assistant.memory.limits.MemoryLimits` is one: the due predicate
is a pure function of stored state and these numbers, and keeping it free of
the configuration layer is what lets it be tested against a table of
conversations instead of against a parsed YAML file.

The two windows are separate because they answer different questions. The idle
window asks "has this discussion settled", and a settled discussion looks
different on each interface: web and iOS chat are turn-taking, Telegram is
bursty and a member replying twenty minutes later is still the same exchange.
The maximum deferral asks "has this conversation waited too long", and is what
guarantees a busy group chat that never goes quiet is still reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Mapping

WEB_INTERFACE = "web"
"""Web chat, which the iOS chat app also identifies itself as."""

TELEGRAM_INTERFACE = "telegram"

DEFAULT_CONTRIBUTING_INTERFACES: frozenset[str] = frozenset({
    WEB_INTERFACE,
    TELEGRAM_INTERFACE,
})
"""The household-member interfaces whose conversations feed the curator.

Spoken interfaces are deliberately absent, for reasons in the persistence layer
rather than in the design (docs/design/conversation-memory.md, "Whose memory it
is"): a telephone call is saved as a transcript note rather than as message
rows, and an iOS native-voice session is persisted with every assistant row
stamped at the untrusted extreme, so every such stretch would trip the
provenance rule. Each becomes a contributor when its persistence carries
message rows with real provenance.
"""


@dataclass(frozen=True)
class MemoryReviewSettings:
    """What the sweep and the due predicate read, in force for one process.

    Attributes:
        enabled: The master switch. With it off nothing is swept and nothing is
            enqueued, whatever any profile is configured to do.
        sweep_interval_minutes: How often the recurring system task evaluates
            the predicate. Freshness is quantised to this, which is negligible
            against the idle windows below.
        idle_window_minutes: Per interface, how long a conversation must have
            been quiet before it is reviewed.
        default_idle_window_minutes: The window for an interface the map does
            not name.
        max_deferral_hours: How long the oldest unreviewed eligible row may
            wait before the conversation is reviewed whether it is quiet or not.
        contributing_interfaces: Which interfaces may contribute at all.

    Not hashable in practice: the interface map makes the generated ``__hash__``
    raise. Nothing keys on one.
    """

    enabled: bool = True
    sweep_interval_minutes: int = 5
    idle_window_minutes: Mapping[str, int] = field(
        default_factory=lambda: {WEB_INTERFACE: 30, TELEGRAM_INTERFACE: 90}
    )
    default_idle_window_minutes: int = 30
    max_deferral_hours: int = 24
    contributing_interfaces: frozenset[str] = DEFAULT_CONTRIBUTING_INTERFACES

    DEFAULTS: ClassVar[MemoryReviewSettings]

    def idle_window(self, interface_type: str) -> timedelta:
        """How long ``interface_type`` must be quiet before a review."""
        return timedelta(
            minutes=self.idle_window_minutes.get(
                interface_type, self.default_idle_window_minutes
            )
        )

    @property
    def max_deferral(self) -> timedelta:
        """How long the oldest unreviewed row may wait regardless of activity."""
        return timedelta(hours=self.max_deferral_hours)


MemoryReviewSettings.DEFAULTS = MemoryReviewSettings()
