"""Which turns in the history window the new message continues.

Asked only at a compaction event, once per candidate turn, in a single Jev
request: the new message and each turn's user text and final answer are the
state, and each question asks whether the new message continues that turn. The
compaction keeps the turns that answer yes verbatim in preference to the rest;
the budget itself is enforced in code, so the classifier only chooses which
turns fill it. See docs/design/history-compaction.md, "Relevance at compaction
events".

Every question reads the same state, so text injected into one candidate turn
can move every answer in the request. What the answers control bounds that: at
worst the wrong turns stay verbatim within the budget. A timeout or failure
returns no preference, and the event proceeds oldest-first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from family_assistant.llm.model_routing import bounded_text
from family_assistant.llm.typesafe import JevClient, NoulQuestion, TypeSafeError

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

RelevanceMode = Literal["shadow", "active"]
RelevanceOutcomeKind = Literal["decided", "timeout", "error"]

_REQUEST_CHARS = 2_000
_TURN_TEXT_CHARS = 800


@dataclass(frozen=True, slots=True)
class RelevanceOutcome:
    """The classifier's answers, and whether the compaction may act on them."""

    outcome: RelevanceOutcomeKind
    active: bool
    model: str
    latency_ms: int
    probabilities: dict[str, float] = field(default_factory=dict)
    threshold: float = 0.5

    @property
    def relevant_keys(self) -> frozenset[str]:
        return frozenset(
            key
            for key, probability in self.probabilities.items()
            if probability >= self.threshold
        )

    def to_json(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "active": self.active,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "threshold": self.threshold,
            "probabilities": self.probabilities,
        }


class TurnRelevance(Protocol):
    active: bool
    """Whether compaction acts on the answers, rather than only recording them."""

    async def assess(
        self, *, request: str, turns: Sequence[tuple[str, str, str]]
    ) -> RelevanceOutcome:
        """Rank ``(key, user_text, final_answer)`` turns against *request*."""
        ...


class JevTurnRelevance:
    """Asks Jev, one yes/no question per candidate turn. Never raises."""

    def __init__(
        self,
        client: JevClient,
        *,
        mode: RelevanceMode,
        threshold: float,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self.active = mode == "active"
        self._threshold = threshold
        self._timeout_seconds = timeout_seconds

    async def assess(
        self, *, request: str, turns: Sequence[tuple[str, str, str]]
    ) -> RelevanceOutcome:
        started = time.monotonic()
        labels = {f"turn_{index}": key for index, (key, _, _) in enumerate(turns)}
        state = {
            "new_message": bounded_text(request, _REQUEST_CHARS),
            "earlier_turns": {
                label: {
                    "user": bounded_text(user_text, _TURN_TEXT_CHARS),
                    "assistant": bounded_text(answer, _TURN_TEXT_CHARS),
                }
                for label, (_, user_text, answer) in zip(labels, turns, strict=True)
            },
        }
        questions = {
            label: NoulQuestion(
                instructions=(
                    f"Does `new_message` continue, follow up on, or need the "
                    f"detail of the earlier conversation turn `earlier_turns."
                    f"{label}`?"
                ),
                true_criteria=(
                    "The new message refers back to that turn, asks about the "
                    "same subject, or cannot be answered well without it."
                ),
                false_criteria="The new message is about something else.",
            )
            for label in labels
        }
        try:
            answers = await asyncio.wait_for(
                self._client.ask(state, questions), timeout=self._timeout_seconds
            )
        except TimeoutError:
            logger.warning("Turn relevance timed out; compacting oldest-first.")
            return self._failed("timeout", started)
        except TypeSafeError:
            logger.warning(
                "Turn relevance failed; compacting oldest-first.", exc_info=True
            )
            return self._failed("error", started)
        return RelevanceOutcome(
            outcome="decided",
            active=self.active,
            model=answers.model or self._client.model,
            latency_ms=_elapsed_ms(started),
            probabilities={
                labels[label]: probability
                for label, probability in answers.nouls.items()
                if label in labels
            },
            threshold=self._threshold,
        )

    def _failed(
        self, outcome: RelevanceOutcomeKind, started: float
    ) -> RelevanceOutcome:
        return RelevanceOutcome(
            outcome=outcome,
            active=False,
            model=self._client.model,
            latency_ms=_elapsed_ms(started),
            threshold=self._threshold,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
