"""A client for TypeSafe's Jev classifier.

Jev answers typed questions about one ``state`` in a single request: a ``noul``
question returns the probability of yes, a ``choice`` question a probability per
option. It has no tools and its answers are schema-constrained, so whatever the
state contains can only move probabilities between the answers the caller
offered. See https://docs.typesafe.ai/api.

The client never retries: its callers have a deterministic fallback for a failed
or slow answer, and a retry would only spend the latency budget that fallback
exists to protect.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx

from family_assistant.observability.metrics import (
    current_call_attribution,
    record_llm_call,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.llm.messages import MessageReasoningInfo

logger = logging.getLogger(__name__)

TYPESAFE_BASE_URL = "https://api.typesafe.ai"


class TypeSafeError(Exception):
    """Jev did not return a usable answer: an HTTP error, a timeout or a bad body."""


@dataclass(frozen=True, slots=True)
class NoulQuestion:
    """A yes/no question; the answer is the probability of yes."""

    instructions: str
    true_criteria: str | None = None
    false_criteria: str | None = None

    def to_json(self) -> dict[str, object]:
        question: dict[str, object] = {
            "type": "noul",
            "instructions": self.instructions,
        }
        if self.true_criteria is not None or self.false_criteria is not None:
            question["criteria"] = {
                "true": self.true_criteria,
                "false": self.false_criteria,
            }
        return question


@dataclass(frozen=True, slots=True)
class ChoiceQuestion:
    """A choice among options, each with an optional rubric."""

    instructions: str | Mapping[str, object]
    options: Mapping[str, str | None]

    def to_json(self) -> dict[str, object]:
        return {
            "type": "choice",
            "instructions": self.instructions,
            "criteria": dict(self.options),
        }


@dataclass(frozen=True, slots=True)
class JevAnswers:
    """Answers by question key: a probability for a noul, a distribution for a choice."""

    model: str
    nouls: dict[str, float]
    choices: dict[str, dict[str, float]]
    input_tokens: int | None
    output_tokens: int | None = None


class JevClient:
    """Calls ``POST /v1/systemone``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        base_url: str = TYPESAFE_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def ask(
        self,
        state: str | Mapping[str, object],
        questions: Mapping[str, NoulQuestion | ChoiceQuestion],
    ) -> JevAnswers:
        """Evaluate *questions* against *state*. Raises :class:`TypeSafeError`.

        Every call is counted in the LLM call and token metrics under the
        current attribution, like any provider call, whether it answers, fails
        or is abandoned by its caller's timeout.
        """
        started = time.monotonic()
        try:
            answers = await self._ask(state, questions)
        except TypeSafeError as error:
            self._record(started, "error", type(error).__name__, None)
            raise
        except asyncio.CancelledError:
            self._record(started, "cancelled", None, None)
            raise
        self._record(started, "success", None, answers)
        return answers

    def _record(
        self,
        started: float,
        outcome: str,
        error_type: str | None,
        answers: JevAnswers | None,
    ) -> None:
        usage: MessageReasoningInfo | None = None
        if answers is not None:
            usage = {}
            if answers.input_tokens is not None:
                usage["prompt_tokens"] = answers.input_tokens
            if answers.output_tokens is not None:
                usage["completion_tokens"] = answers.output_tokens
        record_llm_call(
            attribution=current_call_attribution(),
            provider="typesafe",
            model=self.model,
            resolved_model=answers.model if answers is not None else None,
            operation="classify",
            outcome=outcome,
            error_type=error_type,
            duration_seconds=time.monotonic() - started,
            time_to_first_output_seconds=None,
            reasoning_info=usage,
        )

    async def _ask(
        self,
        state: str | Mapping[str, object],
        questions: Mapping[str, NoulQuestion | ChoiceQuestion],
    ) -> JevAnswers:
        try:
            response = await self._client.post(
                "/v1/systemone",
                json={
                    "model": self.model,
                    "state": state,
                    "questions": {
                        key: question.to_json() for key, question in questions.items()
                    },
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TypeSafeError(f"Jev request failed: {error}") from error
        return _parse_answers(body, questions)


def _parse_answers(
    body: object, questions: Mapping[str, NoulQuestion | ChoiceQuestion]
) -> JevAnswers:
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise TypeSafeError(f"Jev returned no answers: {body!r}")
    answers: dict[str, object] = body["answers"]
    nouls: dict[str, float] = {}
    choices: dict[str, dict[str, float]] = {}
    for key, question in questions.items():
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise TypeSafeError(f"Jev returned no answer for {key!r}")
        kind: Literal["noul", "choice"] = (
            "noul" if isinstance(question, NoulQuestion) else "choice"
        )
        if kind == "noul":
            nouls[key] = _probability(answer.get("noul"), key)
        else:
            assert isinstance(question, ChoiceQuestion)
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict):
                raise TypeSafeError(f"Jev answer for {key!r} has no distribution")
            distribution = {
                str(option): _probability(value, key)
                for option, value in probabilities.items()
            }
            # The API answers with every offered option and nothing else; any
            # other answer is broken, and a caller must not act on a label it
            # never offered or miss one it did.
            if set(distribution) != set(question.options):
                raise TypeSafeError(
                    f"Jev answer for {key!r} does not cover exactly the offered "
                    f"options: {sorted(distribution)}"
                )
            choices[key] = distribution
    usage = body.get("usage")
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    return JevAnswers(
        model=str(body.get("model", "")),
        nouls=nouls,
        choices=choices,
        input_tokens=input_tokens if isinstance(input_tokens, int) else None,
        output_tokens=output_tokens if isinstance(output_tokens, int) else None,
    )


def _probability(value: object, key: str) -> float:
    """*value* as a probability, or :class:`TypeSafeError` if it is not one.

    A value outside the 0-1 scale is a broken answer, not an emphatic one, and
    must take the caller's fallback rather than clear any threshold. ``bool``
    is excluded explicitly because it is an ``int`` to Python.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise TypeSafeError(f"Jev answer for {key!r} is not a probability: {value!r}")
    return float(value)
