"""Jev as a relevance filter over a search surface's candidates.

Each candidate gets its own yes/no question against a shared state, so the
answers are calibrated probabilities rather than a relative order: a threshold
on them is both the filter and the cut point, and sorting by them is the
rerank. Candidates go in batches of ``_BATCH_SIZE`` per request, concurrently,
to stay well inside Jev's state budget. See docs/design/jev-search-filtering.md.

Everything in a batch reads the same state, so text injected into one candidate
can move every answer in that batch. What the answers control bounds that: at
worst the wrong candidates surface, among those the surface's own scoring
already retrieved. A timeout or failure returns no answers, and the caller
keeps its own scoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from family_assistant.llm.typesafe import JevAnswers, NoulQuestion, TypeSafeError
from family_assistant.observability.metrics import record_jev_filter

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from family_assistant.config_models import JevFilterConfig
    from family_assistant.llm.typesafe import JevClient
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

FilterSurface = Literal["calendar_duplicates", "calendar_search", "document_search"]
FilterOutcomeKind = Literal["decided", "timeout", "error"]

_BATCH_SIZE = 30


@dataclass(frozen=True, slots=True)
class CandidateQuestion:
    """One yes/no question, asked once per candidate.

    ``instructions`` names the candidate as ``{candidate}``, which becomes its
    path in the state (``candidates.c03``).
    """

    instructions: str
    true_criteria: str
    false_criteria: str

    def for_label(self, label: str) -> NoulQuestion:
        return NoulQuestion(
            instructions=self.instructions.format(candidate=f"candidates.{label}"),
            true_criteria=self.true_criteria,
            false_criteria=self.false_criteria,
        )


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    """Jev's answers by candidate key, and whether the caller may act on them."""

    outcome: FilterOutcomeKind
    active: bool
    threshold: float
    latency_ms: int
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def applies(self) -> bool:
        """Whether the caller should use these answers in place of its own."""
        return self.active and self.outcome == "decided"

    def kept(self) -> list[str]:
        """Keys at or above the threshold, most probable first."""
        return sorted(
            (
                key
                for key, probability in self.probabilities.items()
                if probability >= self.threshold
            ),
            key=lambda key: self.probabilities[key],
            reverse=True,
        )


class JevCandidateFilter:
    """Asks Jev about every candidate of one surface. Never raises."""

    def __init__(
        self,
        client: JevClient,
        *,
        surface: FilterSurface,
        config: JevFilterConfig,
    ) -> None:
        self._client = client
        self._surface: FilterSurface = surface
        self._mode = config.mode
        self._threshold = config.threshold
        self._timeout_seconds = config.timeout_seconds

    @property
    def threshold(self) -> float:
        return self._threshold

    async def assess(
        self,
        *,
        query: Mapping[str, object],
        candidates: Mapping[str, Mapping[str, object]],
        question: CandidateQuestion,
        baseline: Collection[str],
    ) -> FilterOutcome:
        """Ask *question* about each of *candidates*, keyed by caller key.

        *query* is the state every question shares. *baseline* is what the
        surface's own scoring keeps; it is only recorded, as what Jev's answer
        is measured against.
        """
        started = time.monotonic()
        keys = list(candidates)
        labels = {f"c{index:02d}": key for index, key in enumerate(keys)}
        batches = [
            list(labels)[start : start + _BATCH_SIZE]
            for start in range(0, len(labels), _BATCH_SIZE)
        ]
        try:
            answers = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        self._client.ask(
                            {
                                **query,
                                "candidates": {
                                    label: candidates[labels[label]] for label in batch
                                },
                            },
                            {label: question.for_label(label) for label in batch},
                        )
                        for batch in batches
                    )
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Jev %s filter timed out; using the surface's own scoring.",
                self._surface,
            )
            return self._finish("timeout", started, {}, baseline)
        except TypeSafeError:
            logger.warning(
                "Jev %s filter failed; using the surface's own scoring.",
                self._surface,
                exc_info=True,
            )
            return self._finish("error", started, {}, baseline)
        return self._finish("decided", started, _by_key(answers, labels), baseline)

    def _finish(
        self,
        outcome: FilterOutcomeKind,
        started: float,
        probabilities: dict[str, float],
        baseline: Collection[str],
    ) -> FilterOutcome:
        result = FilterOutcome(
            outcome=outcome,
            active=self._mode == "active",
            threshold=self._threshold,
            latency_ms=int((time.monotonic() - started) * 1000),
            probabilities=probabilities,
        )
        kept = set(result.kept())
        change = _change(set(baseline), kept) if outcome == "decided" else "none"
        record_jev_filter(
            surface=self._surface,
            mode=self._mode,
            outcome=outcome,
            change=change,
            latency_seconds=result.latency_ms / 1000,
        )
        # One line per run carries everything shadow evaluation needs, in the
        # plain-text log format: what each side kept and Jev's probabilities.
        logger.info(
            "Jev %s filter (%s): %s in %dms, change=%s %s",
            self._surface,
            self._mode,
            outcome,
            result.latency_ms,
            change,
            json.dumps(
                {
                    "threshold": self._threshold,
                    "baseline": sorted(baseline),
                    "kept": sorted(kept),
                    "probabilities": {
                        key: round(value, 3) for key, value in probabilities.items()
                    },
                },
                sort_keys=True,
            ),
        )
        return result


def _by_key(answers: list[JevAnswers], labels: dict[str, str]) -> dict[str, float]:
    return {
        labels[label]: probability
        for answer in answers
        for label, probability in answer.nouls.items()
        if label in labels
    }


def _change(baseline: set[str], kept: set[str]) -> str:
    if kept == baseline:
        return "same"
    if kept < baseline:
        return "narrowed"
    if kept > baseline:
        return "widened"
    return "different"


def candidate_filter_for(
    exec_context: ToolExecutionContext, surface: FilterSurface
) -> JevCandidateFilter | None:
    """The deployment's Jev filter for *surface*, unless Jev is absent or off."""
    service = exec_context.processing_service
    if service is None or service.jev_client is None:
        return None
    typesafe = service.app_config.typesafe
    match surface:
        case "calendar_duplicates":
            config = typesafe.calendar_duplicates
        case "calendar_search":
            config = typesafe.calendar_search
        case "document_search":
            config = typesafe.document_search
    if config.mode == "off":
        return None
    return JevCandidateFilter(service.jev_client, surface=surface, config=config)
